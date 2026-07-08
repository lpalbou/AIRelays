// Generates the desktop tray icons (run on macOS during asset updates):
//
//   swift make_tray_icons.swift <output-dir>
//
// Produces square 44x44 px (@2x for a 22 pt tray slot) PNGs:
// - tray-connected.png / tray-disconnected.png (green / red, Windows+Linux)
// - tray-connected-template.png / tray-disconnected-template.png
//   (black, macOS template rendering)
//
// State is encoded in shape as well as color: connected adds relay arcs
// around the bolt, so colorblind users can still tell the difference.

import AppKit
import CoreGraphics
import Foundation

let arguments = CommandLine.arguments
guard arguments.count == 2 else {
    FileHandle.standardError.write(Data("usage: swift make_tray_icons.swift <output-dir>\n".utf8))
    exit(1)
}
let outputDir = URL(fileURLWithPath: arguments[1])
try FileManager.default.createDirectory(at: outputDir, withIntermediateDirectories: true)

let size = 44

func boltPath(in rect: CGRect) -> CGPath {
    let points: [CGPoint] = [
        CGPoint(x: 0.585, y: 1.000),
        CGPoint(x: 0.115, y: 0.425),
        CGPoint(x: 0.410, y: 0.425),
        CGPoint(x: 0.300, y: 0.000),
        CGPoint(x: 0.885, y: 0.575),
        CGPoint(x: 0.520, y: 0.575),
    ]
    let path = CGMutablePath()
    for (index, unit) in points.enumerated() {
        let point = CGPoint(x: rect.minX + unit.x * rect.width, y: rect.minY + unit.y * rect.height)
        index == 0 ? path.move(to: point) : path.addLine(to: point)
    }
    path.closeSubpath()
    return path
}

func render(color: CGColor, arcs: Bool, glow: Bool) -> CGImage {
    let context = CGContext(
        data: nil, width: size, height: size, bitsPerComponent: 8, bytesPerRow: 0,
        space: CGColorSpace(name: CGColorSpace.sRGB)!,
        bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
    )!
    let canvas = CGFloat(size)
    let center = CGPoint(x: canvas / 2, y: canvas / 2)

    // "Blooming" state: a soft colored halo behind the glyph.
    if glow {
        context.saveGState()
        context.setShadow(offset: .zero, blur: canvas * 0.22, color: color)
    }

    // Bolt fills the height; narrower when arcs flank it.
    let boltWidth: CGFloat = arcs ? 17 : 20
    let boltRect = CGRect(x: (canvas - boltWidth) / 2, y: 3, width: boltWidth, height: canvas - 6)
    // Double pass strengthens the halo.
    for _ in 0..<(glow ? 2 : 1) {
        context.addPath(boltPath(in: boltRect))
        context.setFillColor(color)
        context.fillPath()
    }

    if arcs {
        context.setStrokeColor(color)
        context.setLineWidth(3.2)
        context.setLineCap(.round)
        let halfSpan = CGFloat.pi * 0.17
        for baseAngle in [CGFloat.pi, CGFloat(0)] {
            context.addArc(
                center: center, radius: canvas * 0.40,
                startAngle: baseAngle - halfSpan, endAngle: baseAngle + halfSpan,
                clockwise: false
            )
            context.strokePath()
        }
    }
    if glow {
        context.restoreGState()
    }
    return context.makeImage()!
}

func write(_ image: CGImage, _ name: String) {
    let rep = NSBitmapImageRep(cgImage: image)
    let data = rep.representation(using: .png, properties: [:])!
    try! data.write(to: outputDir.appendingPathComponent(name))
}

let green = CGColor(red: 0.24, green: 0.86, blue: 0.42, alpha: 1)
let red = CGColor(red: 1.00, green: 0.30, blue: 0.26, alpha: 1)
let black = CGColor(red: 0, green: 0, blue: 0, alpha: 1)
// Activity pulse: a near-white flash of the connected glyph, shown for a
// fraction of a second when the relay serves a request.
let flash = CGColor(red: 0.95, green: 1.00, blue: 0.85, alpha: 1)

write(render(color: green, arcs: true, glow: true), "tray-connected.png")
write(render(color: red, arcs: false, glow: false), "tray-disconnected.png")
write(render(color: flash, arcs: true, glow: true), "tray-pulse.png")
write(render(color: black, arcs: true, glow: false), "tray-connected-template.png")
write(render(color: black, arcs: false, glow: false), "tray-disconnected-template.png")
print(outputDir.path)
