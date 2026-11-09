// Personen schwarz-weiß, Raum bleibt farbig: people SAT bild1.jpg [bild2.jpg ...]
// Apple Vision Personen-Kontur (weich gerechnet) -> darin Sättigung SAT und Lichter gedämpft. Bild wird überschrieben.
import CoreImage
import Foundation
import Vision

let args = CommandLine.arguments
let sat = Double(args[1])!
let ctx = CIContext()
var hits = 0
for path in args.dropFirst(2) { try autoreleasepool {  // ohne Pool wächst der Speicher je Bild (bei ~900 Frames > 8 GB)
    let url = URL(fileURLWithPath: path)
    guard let img = CIImage(contentsOf: url) else { return }
    let handler = VNImageRequestHandler(url: url)
    let req = VNGeneratePersonInstanceMaskRequest()
    try? handler.perform([req])
    guard let r = req.results?.first, !r.allInstances.isEmpty,
          let buf = try? r.generateScaledMaskForImage(forInstances: r.allInstances, from: handler) else { return }
    hits += 1
    var mask = CIImage(cvPixelBuffer: buf)
    mask = mask.transformed(by: CGAffineTransform(scaleX: img.extent.width / mask.extent.width,
                                                  y: img.extent.height / mask.extent.height))
        .clampedToExtent().applyingGaussianBlur(sigma: 6).cropped(to: img.extent)
    let calm = img.applyingFilter("CIColorControls", parameters: [kCIInputSaturationKey: sat])
        .applyingFilter("CIHighlightShadowAdjust", parameters: ["inputHighlightAmount": 0.75])
    let out = calm.applyingFilter("CIBlendWithMask", parameters: [
        kCIInputBackgroundImageKey: img, kCIInputMaskImageKey: mask])
    try ctx.writeJPEGRepresentation(of: out, to: url, colorSpace: img.colorSpace ?? CGColorSpace(name: CGColorSpace.sRGB)!,
                                    options: [CIImageRepresentationOption(rawValue: kCGImageDestinationLossyCompressionQuality as String): 0.95])
} }
print("Personen in \(hits) Bildern schwarz-weiß")
