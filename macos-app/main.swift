// WhatsApp Bridge.app owns the bridge's launch agent. The agent's plist ships
// inside the bundle at Contents/Library/LaunchAgents/<bundle id>.plist and runs
// the bundled bridge (Contents/MacOS/whatsapp-bridge) through BundleProgram;
// this executable registers it with SMAppService and reports its state.
//
// Opened from Finder (no arguments) it registers the agent and shows the result.
// From a terminal: `WhatsApp Bridge register | status | unregister`, printing
// one JSON object. Exit status: 0 enabled and healthy (or unregistered),
// 2 waiting for approval in System Settings, 1 anything else.

import AppKit
import Foundation
import ServiceManagement

let bundleID = Bundle.main.bundleIdentifier ?? "com.pedro.whatsapp-bridge"
let plistName = "\(bundleID).plist"
let service = SMAppService.agent(plistName: plistName)
let agentPlistURL = Bundle.main.bundleURL
    .appendingPathComponent("Contents/Library/LaunchAgents")
    .appendingPathComponent(plistName)

func statusName(_ status: SMAppService.Status) -> String {
    switch status {
    case .notRegistered: return "notRegistered"
    case .enabled: return "enabled"
    case .requiresApproval: return "requiresApproval"
    case .notFound: return "notFound"
    @unknown default: return "unknown(\(status.rawValue))"
    }
}

/// What launchd runs for the label in this GUI session: the running process's
/// executable when the job is up, else the program launchd reports for it.
/// SMAppService reports a status by label, so this tells whether the loaded job
/// is really the agent inside this bundle and not another plist with the same label.
func loadedProgram() -> String? {
    let process = Process()
    process.executableURL = URL(fileURLWithPath: "/bin/launchctl")
    process.arguments = ["print", "gui/\(getuid())/\(bundleID)"]
    let pipe = Pipe()
    process.standardOutput = pipe
    process.standardError = FileHandle.nullDevice
    do { try process.run() } catch { return nil }
    let output = String(data: pipe.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
    process.waitUntilExit()
    guard process.terminationStatus == 0 else { return nil }

    var pid: Int32?
    var program: String?
    var bundleProgram: String?
    var parentBundle: String?
    for line in output.split(separator: "\n") {
        let trimmed = line.trimmingCharacters(in: .whitespaces)
        if pid == nil, trimmed.hasPrefix("pid = ") {
            pid = Int32(trimmed.dropFirst("pid = ".count))
        } else if program == nil, trimmed.hasPrefix("program = ") {
            program = String(trimmed.dropFirst("program = ".count))
        } else if bundleProgram == nil, trimmed.hasPrefix("program identifier = ") {
            // "program identifier = Contents/MacOS/whatsapp-bridge (mode: 2)" for a BundleProgram job
            bundleProgram = trimmed.dropFirst("program identifier = ".count)
                .components(separatedBy: " (mode:").first
        } else if parentBundle == nil, trimmed.hasPrefix("parent bundle identifier = ") {
            parentBundle = String(trimmed.dropFirst("parent bundle identifier = ".count))
        }
    }
    if program == nil, let bundleProgram, parentBundle == bundleID {
        program = Bundle.main.bundleURL.appendingPathComponent(bundleProgram).path
    }
    if let pid, pid > 0 {
        var path = [CChar](repeating: 0, count: Int(MAXPATHLEN))
        if proc_pidpath(pid, &path, UInt32(path.count)) > 0 {
            return String(cString: path)
        }
    }
    return program
}

func ownsJob(_ program: String?) -> Bool {
    guard let program else { return false }
    return program.hasPrefix(Bundle.main.bundleURL.resolvingSymlinksInPath().path + "/")
        || program.hasPrefix(Bundle.main.bundlePath + "/")
}

/// The bridge's HTTP port, read from the bundled agent's environment.
func bridgePort() -> String {
    guard
        let plist = NSDictionary(contentsOf: agentPlistURL),
        let environment = plist["EnvironmentVariables"] as? [String: String],
        let port = environment["WHATSAPP_MCP_HTTP_PORT"]
    else { return "18080" }
    return port
}

/// /api/health as a dictionary, or nil while the bridge is not answering.
func health() -> [String: Any]? {
    guard let url = URL(string: "http://127.0.0.1:\(bridgePort())/api/health") else { return nil }
    var request = URLRequest(url: url)
    request.timeoutInterval = 2
    let done = DispatchSemaphore(value: 0)
    var result: [String: Any]?
    URLSession.shared.dataTask(with: request) { data, _, _ in
        if let data, let value = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            result = value
        }
        done.signal()
    }.resume()
    _ = done.wait(timeout: .now() + 3)
    return result
}

func isHealthy(_ value: [String: Any]?) -> Bool {
    (value?["ok"] as? Bool) == true && (value?["logged_in"] as? Bool) == true
}

/// Waits up to `seconds` for the bridge to report ok and logged in.
func waitForHealth(seconds: Int) -> [String: Any]? {
    var last: [String: Any]?
    for _ in 0..<max(1, seconds) {
        last = health()
        if isHealthy(last) { return last }
        Thread.sleep(forTimeInterval: 1)
    }
    return last
}

struct Report {
    var action: String
    var status: SMAppService.Status = service.status
    var program: String? = loadedProgram()
    var health: [String: Any]?
    var error: String?
    var openedLoginItems = false

    var owned: Bool { ownsJob(program) }

    var exitCode: Int32 {
        if error != nil { return 1 }
        switch status {
        case .requiresApproval: return 2
        case .enabled: return owned && isHealthy(health) ? 0 : 1
        case .notRegistered: return action == "unregister" ? 0 : 1
        default: return 1
        }
    }

    var json: String {
        var object: [String: Any] = [
            "action": action,
            "label": bundleID,
            "agent_plist": agentPlistURL.path,
            "status": statusName(status),
            "healthy": isHealthy(health),
            "job_owned_by_app": owned,
        ]
        if let program { object["program"] = program }
        if let health { object["health"] = health }
        if let error { object["error"] = error }
        if openedLoginItems { object["opened_login_items_settings"] = true }
        let data = (try? JSONSerialization.data(withJSONObject: object, options: [.prettyPrinted, .sortedKeys])) ?? Data()
        return String(data: data, encoding: .utf8) ?? "{}"
    }

    var summary: String {
        if let error { return "The WhatsApp bridge agent could not be \(action == "unregister" ? "removed" : "registered"): \(error)" }
        switch status {
        case .enabled where !owned:
            return "Another launch agent with the label \(bundleID) is loaded (\(program ?? "no program")). Boot it out and remove its plist, then open the app again."
        case .enabled where isHealthy(health):
            return "The WhatsApp bridge is running and logged in."
        case .enabled:
            return "The WhatsApp bridge agent is enabled, but the bridge is not answering as logged in yet. Check ~/Library/Logs/whatsapp-bridge.log."
        case .requiresApproval:
            return "macOS needs your approval: turn on WhatsApp Bridge under System Settings > General > Login Items & Extensions, then open the app again."
        case .notRegistered:
            return action == "unregister" ? "The WhatsApp bridge agent is no longer registered." : "The WhatsApp bridge agent is not registered."
        default:
            return "The WhatsApp bridge agent is \(statusName(status))."
        }
    }
}

/// kSMErrorAlreadyRegistered: registering an agent that is already registered.
let alreadyRegistered = 12

/// A plain plist with the same label. Background Task Management keys items by
/// label, so it must be gone (not just booted out) before the app registers.
let plainPlist = FileManager.default.homeDirectoryForCurrentUser
    .appendingPathComponent("Library/LaunchAgents/\(plistName)")

func register() -> Report {
    var report = Report(action: "register")
    if report.program != nil && !report.owned {
        report.error = "the label \(bundleID) is already loaded from \(report.program ?? "another plist"); boot that job out and remove its plist first"
        return report
    }
    if FileManager.default.fileExists(atPath: plainPlist.path) {
        report.error = "\(plainPlist.path) uses the same label; boot it out and remove it first"
        return report
    }
    do {
        try service.register()
    } catch let error as NSError where error.code == alreadyRegistered {
        // Already registered: report the current state.
    } catch {
        report.error = "\(error.localizedDescription) [\((error as NSError).domain) \((error as NSError).code)]"
    }
    report.status = service.status
    if report.status == .requiresApproval {
        SMAppService.openSystemSettingsLoginItems()
        report.openedLoginItems = true
        report.error = nil
    }
    if report.status == .enabled {
        report.health = waitForHealth(seconds: 45)
        report.program = loadedProgram()
    }
    return report
}

func unregister() -> Report {
    var report = Report(action: "unregister")
    do {
        try service.unregister()
    } catch {
        report.error = "\(error.localizedDescription) [\((error as NSError).domain) \((error as NSError).code)]"
    }
    report.status = service.status
    report.program = loadedProgram()
    return report
}

func status() -> Report {
    var report = Report(action: "status")
    report.health = health()
    return report
}

let arguments = Array(CommandLine.arguments.dropFirst())
if arguments.isEmpty {
    // Opened from Finder or `open`: register and show the outcome.
    let report = register()
    let app = NSApplication.shared
    app.setActivationPolicy(.accessory)
    app.activate(ignoringOtherApps: true)
    let alert = NSAlert()
    alert.messageText = "WhatsApp Bridge"
    alert.informativeText = report.summary
    alert.alertStyle = report.exitCode == 0 ? .informational : .warning
    alert.runModal()
    exit(report.exitCode)
}

let report: Report
switch arguments[0] {
case "register": report = register()
case "unregister": report = unregister()
case "status": report = status()
default:
    FileHandle.standardError.write("usage: WhatsApp Bridge [register | status | unregister]\n".data(using: .utf8)!)
    exit(64)
}
print(report.json)
exit(report.exitCode)
