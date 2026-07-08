// Icon generation for AIRelaysMenuBar.
//
// Usage: swift make_icons.swift <source-artwork.png> <output-dir>
//
// Produces:
// - AppIcon.iconset/ with all macOS icon sizes (squircle-masked, 1024 grid)
// - menu_bar_icon_connected.png / menu_bar_icon_disconnected.png
//   (44x36 px color glyphs rendered @2x for an 18 pt menu bar slot:
//    green bolt with relay arcs when connected, red bolt when not)
//
// The app icon follows the Big Sur icon grid: a 1024 canvas with an
// 824x824 rounded-rect body (corner radius ~185) so Finder renders it
// consistently next to system apps.

import AppKit
import CoreGraphics
import Foundation

let arguments = CommandLine.arguments
guard arguments.count == 3 else {
    FileHandle.standardError.write(Data("usage: swift make_icons.swift <source.png> <output-dir>\n".utf8))
    exit(1)
}

let sourceURL = URL(fileURLWithPath: arguments[1])
let outputDir = URL(fileURLWithPath: arguments[2])
try FileManager.default.createDirectory(at: outputDir, withIntermediateDirectories: true)

func loadCGImage(from url: URL) -> CGImage {
    guard let dataProvider = CGDataProvider(url: url as CFURL),
          let image = CGImage(pngDataProviderSource: dataProvider, decode: nil, shouldInterpolate: true, intent: .defaultIntent) else {
        fatalError("Cannot load PNG at \(url.path)")
    }
    return image
}

func makeContext(width: Int, height: Int) -> CGContext {
    guard let context = CGContext(
        data: nil,
        width: width,
        height: height,
        bitsPerComponent: 8,
        bytesPerRow: 0,
        space: CGColorSpace(name: CGColorSpace.sRGB)!,
        bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
    ) else {
        fatalError("Cannot create CGContext of size \(width)x\(height)")
    }
    return context
}

func makeContext(size: Int) -> CGContext {
    makeContext(width: size, height: size)
}

func writePNG(_ image: CGImage, to url: URL) {
    let rep = NSBitmapImageRep(cgImage: image)
    guard let data = rep.representation(using: .png, properties: [:]) else {
        fatalError("Cannot encode PNG for \(url.path)")
    }
    try! data.write(to: url)
}

// MARK: - App icon (squircle-masked artwork on the 1024 grid)

func centerCroppedSquare(_ image: CGImage) -> CGImage {
    let side = min(image.width, image.height)
    let cropRect = CGRect(
        x: (image.width - side) / 2,
        y: (image.height - side) / 2,
        width: side,
        height: side
    )
    return image.cropping(to: cropRect)!
}

func renderAppIcon(source: CGImage, canvasSize: Int) -> CGImage {
    let context = makeContext(size: canvasSize)
    let canvas = CGFloat(canvasSize)
    // Big Sur grid: icon body is 824/1024 of the canvas, radius ~185/824 of the body.
    let bodySide = canvas * 824.0 / 1024.0
    let bodyRect = CGRect(
        x: (canvas - bodySide) / 2.0,
        y: (canvas - bodySide) / 2.0,
        width: bodySide,
        height: bodySide
    )
    let cornerRadius = bodySide * 185.4 / 824.0

    // Soft drop shadow so the icon sits like system icons on light backgrounds.
    context.saveGState()
    context.setShadow(
        offset: CGSize(width: 0, height: -canvas * 0.008),
        blur: canvas * 0.02,
        color: CGColor(red: 0, green: 0, blue: 0, alpha: 0.30)
    )
    let shadowPath = CGPath(roundedRect: bodyRect, cornerWidth: cornerRadius, cornerHeight: cornerRadius, transform: nil)
    context.addPath(shadowPath)
    context.setFillColor(CGColor(red: 0.1, green: 0.1, blue: 0.3, alpha: 1))
    context.fillPath()
    context.restoreGState()

    context.saveGState()
    let clipPath = CGPath(roundedRect: bodyRect, cornerWidth: cornerRadius, cornerHeight: cornerRadius, transform: nil)
    context.addPath(clipPath)
    context.clip()
    context.interpolationQuality = .high
    context.draw(source, in: bodyRect)
    context.restoreGState()

    return context.makeImage()!
}

// MARK: - Menu bar glyph (colored bolt with relay arcs)

func boltPath(in rect: CGRect) -> CGPath {
    // Unit-space bolt polygon (y-up): a wide, classic bolt silhouette so the
    // glyph stays legible at menu bar size.
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
        if index == 0 {
            path.move(to: point)
        } else {
            path.addLine(to: point)
        }
    }
    path.closeSubpath()
    return path
}

// Rendered @2x for an 18 pt tall, 22 pt wide menu bar slot.
let menuGlyphWidth = 44
let menuGlyphHeight = 36

func renderMenuBarGlyph(color: CGColor, showArcs: Bool) -> CGImage {
    let context = makeContext(width: menuGlyphWidth, height: menuGlyphHeight)
    let width = CGFloat(menuGlyphWidth)
    let height = CGFloat(menuGlyphHeight)
    let center = CGPoint(x: width / 2, y: height / 2)

    // Bolt fills nearly the full slot height for menu bar legibility.
    let boltRect = CGRect(
        x: (width - 20) / 2,
        y: 2,
        width: 20,
        height: height - 4
    )
    context.addPath(boltPath(in: boltRect))
    context.setFillColor(color)
    context.fillPath()

    if showArcs {
        let arcRadius = width * 0.36
        let lineWidth: CGFloat = 3.4
        context.setStrokeColor(color)
        context.setLineWidth(lineWidth)
        context.setLineCap(.round)
        let halfSpan = CGFloat.pi * 0.18
        for baseAngle in [CGFloat.pi, CGFloat(0)] {
            context.addArc(
                center: center,
                radius: arcRadius,
                startAngle: baseAngle - halfSpan,
                endAngle: baseAngle + halfSpan,
                clockwise: false
            )
            context.strokePath()
        }
    }

    return context.makeImage()!
}

// MARK: - Emit files

let artwork = centerCroppedSquare(loadCGImage(from: sourceURL))

let iconsetDir = outputDir.appendingPathComponent("AppIcon.iconset")
try? FileManager.default.removeItem(at: iconsetDir)
try FileManager.default.createDirectory(at: iconsetDir, withIntermediateDirectories: true)

let iconSpecs: [(name: String, size: Int)] = [
    ("icon_16x16.png", 16),
    ("icon_16x16@2x.png", 32),
    ("icon_32x32.png", 32),
    ("icon_32x32@2x.png", 64),
    ("icon_128x128.png", 128),
    ("icon_128x128@2x.png", 256),
    ("icon_256x256.png", 256),
    ("icon_256x256@2x.png", 512),
    ("icon_512x512.png", 512),
    ("icon_512x512@2x.png", 1024),
]

let masterIcon = renderAppIcon(source: artwork, canvasSize: 1024)
for spec in iconSpecs {
    let context = makeContext(size: spec.size)
    context.interpolationQuality = .high
    context.draw(masterIcon, in: CGRect(x: 0, y: 0, width: spec.size, height: spec.size))
    writePNG(context.makeImage()!, to: iconsetDir.appendingPathComponent(spec.name))
}

// System-palette red/green read clearly on both light and dark menu bars.
let connectedGreen = CGColor(red: 0.20, green: 0.78, blue: 0.35, alpha: 1)
let disconnectedRed = CGColor(red: 1.00, green: 0.23, blue: 0.19, alpha: 1)

writePNG(renderMenuBarGlyph(color: connectedGreen, showArcs: true), to: outputDir.appendingPathComponent("menu_bar_icon_connected.png"))
writePNG(renderMenuBarGlyph(color: disconnectedRed, showArcs: false), to: outputDir.appendingPathComponent("menu_bar_icon_disconnected.png"))

print(outputDir.path)
