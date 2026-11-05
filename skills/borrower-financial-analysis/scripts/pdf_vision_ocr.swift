// Local Apple Vision text recognition. No network or source PDF mutation.
import Foundation
import Vision
import AppKit

func emit(_ object: Any) throws {
    let data = try JSONSerialization.data(withJSONObject: object, options: [.sortedKeys])
    print(String(data: data, encoding: .utf8)!)
}

do {
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.usesLanguageCorrection = false
    if CommandLine.arguments.count == 2 && CommandLine.arguments[1] == "--languages" {
        try emit(["languages": request.supportedRecognitionLanguages(), "revision": request.revision])
    } else {
        guard CommandLine.arguments.count == 3 else {
            throw NSError(domain: "pdf-ocr", code: 1, userInfo: [NSLocalizedDescriptionKey: "Expected image and languages"])
        }
        request.recognitionLanguages = CommandLine.arguments[2].components(separatedBy: "+")
        let url = URL(fileURLWithPath: CommandLine.arguments[1])
        let handler = VNImageRequestHandler(url: url, options: [:])
        try handler.perform([request])
        let observations = request.results ?? []
        let lines = observations.compactMap { $0.topCandidates(1).first }
        try emit(["text": lines.map { $0.string }.joined(separator: "\n"),
                  "minimum_confidence": lines.map { $0.confidence }.min() as Any? ?? NSNull()])
    }
} catch {
    FileHandle.standardError.write(Data(error.localizedDescription.utf8))
    exit(2)
}
