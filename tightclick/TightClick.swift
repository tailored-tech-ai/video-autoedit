// TightClick: нажимает две кнопки в Tight Studio и больше ничего не умеет.
//
//   TightClick setup                 системное окно «выдать Accessibility»
//   TightClick check                 trusted | untrusted
//   TightClick allow [сек]           нажать Allow в окне «Allow Claude Desktop?»
//   TightClick export <имя> [сек]    вписать <имя>.mp4 в окно экспорта и нажать Export here
//
// Ответ одной строкой в stdout: ok | none | trusted | untrusted | no-app | fail:<причина>.
// Accessibility выдаётся только этому приложению, а не терминалу: tight_publish.py
// запускает его через `open`, поэтому macOS проверяет права именно TightClick.

import AppKit
import ApplicationServices

let tightBundleID = "tight.studio"
let args = Array(CommandLine.arguments.dropFirst())

func finish(_ s: String) -> Never {
    print(s)
    fflush(stdout)
    exit(["ok", "trusted", "none"].contains(s) ? 0 : 1)
}

func attr(_ e: AXUIElement, _ name: String) -> AnyObject? {
    var v: AnyObject?
    return AXUIElementCopyAttributeValue(e, name as CFString, &v) == .success ? v : nil
}

func text(_ e: AXUIElement, _ name: String) -> String { attr(e, name) as? String ?? "" }

func tree(_ e: AXUIElement, _ depth: Int = 0) -> [AXUIElement] {
    var all = [e]
    if depth < 15 {
        for c in attr(e, kAXChildrenAttribute as String) as? [AXUIElement] ?? [] {
            all += tree(c, depth + 1)
        }
    }
    return all
}

/// Окно Tight Studio с кнопкой `button`; если задан `mustContain`, в окне должен быть этот текст.
func window(_ app: AXUIElement, button: String, mustContain: String?) -> (button: AXUIElement, all: [AXUIElement])? {
    for w in attr(app, kAXWindowsAttribute as String) as? [AXUIElement] ?? [] {
        let all = tree(w)
        let isButton = { (e: AXUIElement) in
            text(e, kAXRoleAttribute as String) == kAXButtonRole as String
                && text(e, kAXTitleAttribute as String) == button
        }
        guard let b = all.first(where: isButton) else { continue }
        if let must = mustContain {
            let words = all.map { e in
                [kAXTitleAttribute, kAXValueAttribute, kAXDescriptionAttribute]
                    .map { name in text(e, name as String) }.joined(separator: " ")
            }.joined(separator: " ")
            if !words.contains(must) { continue }
        }
        return (b, all)
    }
    return nil
}

guard let mode = args.first else { finish("fail:usage") }

if mode == "check" { finish(AXIsProcessTrusted() ? "trusted" : "untrusted") }
if mode == "setup" {
    let prompt = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary
    finish(AXIsProcessTrustedWithOptions(prompt) ? "trusted" : "untrusted")
}
guard AXIsProcessTrusted() else { finish("untrusted") }
guard let pid = NSRunningApplication.runningApplications(withBundleIdentifier: tightBundleID)
    .first?.processIdentifier else { finish("no-app") }
let app = AXUIElementCreateApplication(pid)

let fileName = mode == "export" && args.count > 1 ? args[1] + ".mp4" : ""
let timeoutArg = mode == "export" ? 2 : 1
let timeout = args.count > timeoutArg ? Double(args[timeoutArg]) ?? 20 : 20
if mode == "export" && fileName == ".mp4" { finish("fail:usage") }
if mode != "allow" && mode != "export" { finish("fail:usage") }

let deadline = Date().addingTimeInterval(timeout)
while Date() < deadline {
    if mode == "allow", let w = window(app, button: "Allow", mustContain: "Claude") {
        AXUIElementPerformAction(w.button, kAXPressAction as CFString)
        finish("ok")
    }
    if mode == "export", let w = window(app, button: "Export here", mustContain: nil) {
        let isName = { (e: AXUIElement) in
            text(e, kAXRoleAttribute as String) == kAXTextFieldRole as String
                && text(e, kAXValueAttribute as String).lowercased().hasSuffix(".mp4")
        }
        guard let field = w.all.first(where: isName) else { finish("fail:no-name-field") }
        AXUIElementSetAttributeValue(field, kAXValueAttribute as CFString, fileName as CFString)
        usleep(300_000)
        guard text(field, kAXValueAttribute as String) == fileName else { finish("fail:name") }
        AXUIElementPerformAction(w.button, kAXPressAction as CFString)
        finish("ok")
    }
    usleep(250_000)
}
finish("none")
