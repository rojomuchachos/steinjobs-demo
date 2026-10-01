// SteinJobs — native Mac wrapper.
//
// Built against the CommandLineTools SDK (15.x), which predates the Liquid
// Glass APIs — but the REAL material is still reachable two ways on a
// macOS 26 host:
//   1. NSToolbar / window chrome are drawn by the SYSTEM, so on Tahoe they
//      wear genuine Liquid Glass with zero API calls from us.
//   2. NSGlassEffectView exists at runtime; NSClassFromString gives us the
//      real class without the new SDK's headers. NSVisualEffectView is the
//      graceful fallback on anything older.
// The web view draws no background of its own; the page (in ?wrapper=1
// mode) goes translucent, so the glass reads through the whole app.

import AppKit
import WebKit

let APP_URL = URL(string: "http://127.0.0.1:8377/?wrapper=1")!
let REPO = FileManager.default.homeDirectoryForCurrentUser
    .appendingPathComponent("Job Pipeline")

func serverIsUp() -> Bool {
    let sock = socket(AF_INET, SOCK_STREAM, 0)
    defer { close(sock) }
    var addr = sockaddr_in()
    addr.sin_family = sa_family_t(AF_INET)
    addr.sin_port = in_port_t(8377).bigEndian
    addr.sin_addr.s_addr = inet_addr("127.0.0.1")
    let r = withUnsafePointer(to: &addr) {
        $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
            Darwin.connect(sock, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
        }
    }
    return r == 0
}

final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate {
    var window: NSWindow!
    var webView: WKWebView!
    var server: Process?
    var toolbarDelegate: ToolbarDelegate!

    func applicationDidFinishLaunching(_ note: Notification) {
        if !serverIsUp() { startServer() }

        let rect = NSRect(x: 0, y: 0, width: 1440, height: 940)
        window = NSWindow(contentRect: rect,
                          styleMask: [.titled, .closable, .miniaturizable, .resizable,
                                      .fullSizeContentView],
                          backing: .buffered, defer: false)
        window.title = "SteinJobs"
        window.titlebarAppearsTransparent = true
        window.titleVisibility = .hidden
        window.toolbarStyle = .unified
        window.isReleasedWhenClosed = false
        window.minSize = NSSize(width: 900, height: 600)
        window.center()
        window.setFrameAutosaveName("JobDestroyerMain")

        // Real Liquid Glass when the class exists (macOS 26+); frosted
        // NSVisualEffectView otherwise. KVC only — no new-SDK symbols.
        let backdrop: NSView
        if let glassClass = NSClassFromString("NSGlassEffectView") as? NSView.Type {
            backdrop = glassClass.init(frame: rect)
        } else {
            let v = NSVisualEffectView(frame: rect)
            v.material = .underWindowBackground
            v.blendingMode = .behindWindow
            v.state = .active
            backdrop = v
        }
        backdrop.autoresizingMask = [.width, .height]

        let conf = WKWebViewConfiguration()
        conf.preferences.setValue(true, forKey: "developerExtrasEnabled")
        webView = WKWebView(frame: rect, configuration: conf)
        webView.navigationDelegate = self
        webView.autoresizingMask = [.width, .height]
        webView.setValue(false, forKey: "drawsBackground")   // page glass shows through

        let root = NSView(frame: rect)
        root.autoresizingMask = [.width, .height]
        root.addSubview(backdrop)
        root.addSubview(webView)
        window.contentView = root

        // System toolbar: on Tahoe these items wear genuine Liquid Glass.
        toolbarDelegate = ToolbarDelegate(app: self)
        let tb = NSToolbar(identifier: "main")
        tb.delegate = toolbarDelegate
        tb.displayMode = .iconAndLabel
        tb.allowsUserCustomization = false
        window.toolbar = tb

        loadWhenReady(attempts: 40)
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    func startServer() {
        let p = Process()
        p.currentDirectoryURL = REPO
        p.executableURL = REPO.appendingPathComponent(".venv/bin/python")
        p.arguments = ["-m", "pipeline.cli", "app"]
        p.standardOutput = FileHandle.nullDevice
        p.standardError = FileHandle.nullDevice
        try? p.run()
        server = p
    }

    func loadWhenReady(attempts: Int) {
        if serverIsUp() {
            webView.load(URLRequest(url: APP_URL))
        } else if attempts > 0 {
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.25) {
                self.loadWhenReady(attempts: attempts - 1)
            }
        }
    }

    func js(_ script: String) { webView.evaluateJavaScript(script, completionHandler: nil) }

    func applicationShouldTerminateAfterLastWindowClosed(_ app: NSApplication) -> Bool { true }
    func applicationWillTerminate(_ note: Notification) { server?.terminate() }
}

final class ToolbarDelegate: NSObject, NSToolbarDelegate {
    weak var app: AppDelegate?
    init(app: AppDelegate) { self.app = app }

    static let tabs: [(id: NSToolbarItem.Identifier, label: String, tab: String, icon: String)] = [
        (.init("tracker"),   "Tracker",   "tracker",   "chart.bar.doc.horizontal"),
        (.init("postings"),  "Postings",  "postings",  "list.bullet.rectangle"),
        (.init("companies"), "Companies", "companies", "building.2"),
        (.init("people"),    "People",    "people",    "person.2"),
    ]
    static let refresh = NSToolbarItem.Identifier("refresh")

    func toolbarAllowedItemIdentifiers(_ t: NSToolbar) -> [NSToolbarItem.Identifier] {
        toolbarDefaultItemIdentifiers(t)
    }
    func toolbarDefaultItemIdentifiers(_ t: NSToolbar) -> [NSToolbarItem.Identifier] {
        // No refresh item: the page's own Find New Roles fab covers it, and a
        // native twin collided with the web header's right-side buttons.
        [.flexibleSpace] + Self.tabs.map { $0.id } + [.flexibleSpace]
    }
    func toolbar(_ t: NSToolbar, itemForItemIdentifier id: NSToolbarItem.Identifier,
                 willBeInsertedIntoToolbar flag: Bool) -> NSToolbarItem? {
        if id == Self.refresh {
            let item = NSToolbarItem(itemIdentifier: id)
            item.label = "Find New Roles"
            item.image = NSImage(systemSymbolName: "sparkles",
                                 accessibilityDescription: "Find New Roles")
            item.target = self
            item.action = #selector(refreshTapped)
            item.isBordered = true
            return item
        }
        guard let spec = Self.tabs.first(where: { $0.id == id }) else { return nil }
        let item = NSToolbarItem(itemIdentifier: id)
        item.label = spec.label
        item.image = NSImage(systemSymbolName: spec.icon,
                             accessibilityDescription: spec.label)
        item.target = self
        item.action = #selector(tabTapped(_:))
        item.isBordered = true
        return item
    }
    @objc func tabTapped(_ sender: NSToolbarItem) {
        guard let spec = Self.tabs.first(where: { $0.id == sender.itemIdentifier }) else { return }
        app?.js("switchTab('\(spec.tab)')")
    }
    @objc func refreshTapped() {
        app?.js("document.getElementById('refresh')?.click()")
    }
}

let app = NSApplication.shared
app.setActivationPolicy(.regular)
let delegate = AppDelegate()
app.delegate = delegate
app.run()
