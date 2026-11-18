#!/usr/bin/env swift
// ocr-pdf.swift: makes a scanned PDF usable for study guides by writing "<name>.txt" next to it,
// with "--- page N ---" markers so quotes can be cited by page. Uses macOS's built-in Vision OCR
// (the engine behind Live Text): nothing to install, nothing leaves the Mac.
// Usage: swift ~/code/scripts/ocr-pdf.swift "file.pdf" ["more.pdf" ...]
// Skips a PDF whose .txt already exists. Two-column pages are read left column first.
import AppKit
import Foundation
import PDFKit
import Vision

func pageText(_ page: PDFPage) throws -> String {
    let box = page.bounds(for: .mediaBox)
    let scale: CGFloat = 2.5  // about 180 dpi: enough for small print, still fast
    let image = page.thumbnail(of: NSSize(width: box.width * scale, height: box.height * scale), for: .mediaBox)
    guard let cg = image.cgImage(forProposedRect: nil, context: nil, hints: nil) else { return "" }
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.usesLanguageCorrection = true
    try VNImageRequestHandler(cgImage: cg, options: [:]).perform([request])
    let obs = request.results ?? []
    // Two columns if most lines are narrower than half the page and both halves have text.
    let narrow = obs.filter { $0.boundingBox.width < 0.45 }
    let twoCol = obs.count > 8 && Double(narrow.count) / Double(obs.count) > 0.7
        && narrow.contains { $0.boundingBox.midX < 0.5 } && narrow.contains { $0.boundingBox.midX >= 0.5 }
    let sorted = obs.sorted { a, b in
        let ca = twoCol && a.boundingBox.midX >= 0.5 ? 1 : 0
        let cb = twoCol && b.boundingBox.midX >= 0.5 ? 1 : 0
        if ca != cb { return ca < cb }
        return a.boundingBox.maxY > b.boundingBox.maxY  // Vision's y grows upward: top line first
    }
    return sorted.compactMap { $0.topCandidates(1).first?.string }.joined(separator: "\n")
}

var failed = 0
for path in CommandLine.arguments.dropFirst() {
    let url = URL(fileURLWithPath: path)
    let out = url.deletingPathExtension().appendingPathExtension("txt")
    if FileManager.default.fileExists(atPath: out.path) {
        print("skip (already has \(out.lastPathComponent)): \(url.lastPathComponent)")
        continue
    }
    guard let doc = PDFDocument(url: url) else {
        print("FAILED to open: \(path)"); failed += 1; continue
    }
    let stamp = ISO8601DateFormatter().string(from: Date()).prefix(10)
    var text = "\(url.deletingPathExtension().lastPathComponent)\n"
    text += "OCR text of \(url.lastPathComponent) (macOS Vision, \(stamp)). Check exact quotes against the PDF page.\n\n"
    do {
        for i in 0..<doc.pageCount {
            guard let page = doc.page(at: i) else { continue }
            text += "--- page \(i + 1) ---\n" + (try pageText(page)) + "\n\n"
        }
        try text.write(to: out, atomically: true, encoding: .utf8)
        print("ok: \(out.lastPathComponent) (\(doc.pageCount) pages, \(text.count) chars)")
    } catch {
        print("FAILED: \(path): \(error.localizedDescription)"); failed += 1
    }
}
exit(failed == 0 ? 0 : 1)
