// frame_ocr: распознаёт текст на кадрах встроенным в macOS Vision, без сети.
//   frame_ocr <картинка>...
// На каждую картинку строка JSON: {"file": ..., "lines": [{"text", "x", "y", "w", "h"}]},
// координаты в долях картинки, y от верхнего края.

import AppKit
import Foundation
import Vision

for path in CommandLine.arguments.dropFirst() {
    var lines: [[String: Any]] = []
    if let image = NSImage(contentsOfFile: path),
       let cg = image.cgImage(forProposedRect: nil, context: nil, hints: nil) {
        let request = VNRecognizeTextRequest()
        request.recognitionLevel = .accurate
        request.usesLanguageCorrection = false
        try? VNImageRequestHandler(cgImage: cg).perform([request])
        for obs in request.results ?? [] {
            guard let text = obs.topCandidates(1).first?.string else { continue }
            let b = obs.boundingBox
            lines.append(["text": text, "x": b.minX, "y": 1 - b.maxY, "w": b.width, "h": b.height])
        }
    }
    let data = try! JSONSerialization.data(withJSONObject: ["file": path, "lines": lines])
    print(String(data: data, encoding: .utf8)!)
    fflush(stdout)
}
