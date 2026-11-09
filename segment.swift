// Personen-Freistellung mit Apple Vision: segment bild1.jpg [bild2.jpg ...] -> bildN.mask.pgm (255 = Person)
// Personen-Instanzen (wie "Motiv freistellen" in Fotos); ohne Person: Vordergrund-Motiv.
import Foundation
import Vision

func pgm(_ buf: CVPixelBuffer, _ out: URL) throws {
    CVPixelBufferLockBaseAddress(buf, .readOnly)
    defer { CVPixelBufferUnlockBaseAddress(buf, .readOnly) }
    let w = CVPixelBufferGetWidth(buf), h = CVPixelBufferGetHeight(buf)
    let row = CVPixelBufferGetBytesPerRow(buf), base = CVPixelBufferGetBaseAddress(buf)!
    let isFloat = CVPixelBufferGetPixelFormatType(buf) == kCVPixelFormatType_OneComponent32Float
    var data = Data("P5\n\(w) \(h)\n255\n".utf8)
    for y in 0..<h {
        if isFloat {
            let p = (base + y * row).assumingMemoryBound(to: Float32.self)
            for x in 0..<w { data.append(UInt8(max(0, min(255, p[x] * 255)))) }
        } else { // OneComponent8
            data.append((base + y * row).assumingMemoryBound(to: UInt8.self), count: w)
        }
    }
    try data.write(to: out)
}

let forceFg = CommandLine.arguments.contains("--fg") // nur Vordergrund-Motiv
for path in CommandLine.arguments.dropFirst() where path != "--fg" {
    let url = URL(fileURLWithPath: path)
    let out = url.deletingPathExtension().appendingPathExtension("mask.pgm")
    let handler = VNImageRequestHandler(url: url)
    let person = VNGeneratePersonInstanceMaskRequest()
    try handler.perform([person])
    if !forceFg, let r = person.results?.first, !r.allInstances.isEmpty {
        try pgm(try r.generateScaledMaskForImage(forInstances: r.allInstances, from: handler), out)
        print("person \(r.allInstances.count) \(out.path)")
        continue
    }
    let fg = VNGenerateForegroundInstanceMaskRequest()
    try handler.perform([fg])
    if let r = fg.results?.first, !r.allInstances.isEmpty {
        try pgm(try r.generateScaledMaskForImage(forInstances: r.allInstances, from: handler), out)
        print("motiv \(r.allInstances.count) \(out.path)")
    } else {
        print("nichts \(path)")
    }
}
