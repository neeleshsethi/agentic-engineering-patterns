import Foundation
import AppKit
import Vision

struct OCRResult {
    let fileName: String
    let text: String
}

func usage() -> Never {
    fputs("Usage: swift ocr_images.swift <input-dir> <output-dir>\n", stderr)
    exit(2)
}

let args = CommandLine.arguments
guard args.count == 3 else { usage() }

let inputURL = URL(fileURLWithPath: args[1], isDirectory: true)
let outputURL = URL(fileURLWithPath: args[2], isDirectory: true)
let fm = FileManager.default

try fm.createDirectory(at: outputURL, withIntermediateDirectories: true)

let allowed = Set(["heic", "heif", "jpg", "jpeg", "png", "tif", "tiff"])
let files = try fm.contentsOfDirectory(
    at: inputURL,
    includingPropertiesForKeys: nil,
    options: [.skipsHiddenFiles]
)
    .filter { allowed.contains($0.pathExtension.lowercased()) }
    .sorted { $0.lastPathComponent.localizedStandardCompare($1.lastPathComponent) == .orderedAscending }

func recognizeText(in imageURL: URL) throws -> String {
    var recognized: [VNRecognizedTextObservation] = []
    var requestError: Error?
    let semaphore = DispatchSemaphore(value: 0)

    let request = VNRecognizeTextRequest { request, error in
        requestError = error
        recognized = request.results as? [VNRecognizedTextObservation] ?? []
        semaphore.signal()
    }
    request.recognitionLevel = .accurate
    request.usesLanguageCorrection = true

    guard let image = NSImage(contentsOf: imageURL) else {
        throw NSError(domain: "OCR", code: 1, userInfo: [NSLocalizedDescriptionKey: "Could not load image"])
    }
    var rect = NSRect(origin: .zero, size: image.size)
    guard let cgImage = image.cgImage(forProposedRect: &rect, context: nil, hints: nil) else {
        throw NSError(domain: "OCR", code: 2, userInfo: [NSLocalizedDescriptionKey: "Could not create CGImage"])
    }

    let handler = VNImageRequestHandler(cgImage: cgImage, options: [:])
    try handler.perform([request])
    semaphore.wait()

    if let requestError {
        throw requestError
    }

    return recognized
        .compactMap { $0.topCandidates(1).first?.string }
        .joined(separator: "\n")
}

var combined: [String] = []
combined.append("# Extracted OCR Text")
combined.append("")
combined.append("Source: `\(inputURL.path)`")
combined.append("")

for file in files {
    do {
        let text = try recognizeText(in: file)
        let baseName = file.deletingPathExtension().lastPathComponent
        let outputFile = outputURL.appendingPathComponent("\(baseName).txt")
        try text.write(to: outputFile, atomically: true, encoding: .utf8)

        combined.append("## \(file.lastPathComponent)")
        combined.append("")
        combined.append(text.isEmpty ? "_No text recognized._" : text)
        combined.append("")

        print("OCR \(file.lastPathComponent): \(text.split(separator: "\n").count) lines")
    } catch {
        let message = "OCR failed for \(file.lastPathComponent): \(error)"
        fputs(message + "\n", stderr)
        combined.append("## \(file.lastPathComponent)")
        combined.append("")
        combined.append("_OCR failed: \(error)_")
        combined.append("")
    }
}

let combinedURL = outputURL.appendingPathComponent("combined.md")
try combined.joined(separator: "\n").write(to: combinedURL, atomically: true, encoding: .utf8)
print("Wrote \(combinedURL.path)")
