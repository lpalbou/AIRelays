// Generates the desktop tray icons (run on macOS during asset updates):
//
//   swift make_tray_icons.swift <output-dir>
//
// Produces square 44x44 px (@2x for a 22 pt tray slot) PNGs:
// - tray-connected.png / tray-disconnected.png (green / red, Windows+Linux)
// - tray-connected-template.png / tray-disconnected-template.png
//   (black, macOS template rendering)
// - tray-pulse-NN.png: the request-activity animation. A fast-attack /
//   slow-decay brightness swell of the connected glyph plus a ripple ring
//   expanding out of the bolt. The peak color stays a saturated luminous
//   green (not white): a near-white flash disappears against light menu
//   bars, which made the old single-frame blink invisible. The last frame
//   converges to the connected icon so the hand-off back to the state
//   icon is seamless.
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
// Must match PULSE_FRAME_COUNT in desktop/src-tauri/src/tray.rs, which
// embeds one PNG per frame.
let pulseFrameCount = 16

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

func makeContext() -> CGContext {
    CGContext(
        data: nil, width: size, height: size, bitsPerComponent: 8, bytesPerRow: 0,
        space: CGColorSpace(name: CGColorSpace.sRGB)!,
        bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
    )!
}

/// Draws the bolt (and, when connected, the relay arcs) with an optional
/// soft halo. `glowPasses` > 1 strengthens the halo by re-filling the bolt.
func drawGlyph(_ context: CGContext, color: CGColor, arcs: Bool, glowBlur: CGFloat, glowPasses: Int) {
    let canvas = CGFloat(size)
    let center = CGPoint(x: canvas / 2, y: canvas / 2)
    context.saveGState()
    if glowBlur > 0 {
        context.setShadow(offset: .zero, blur: glowBlur, color: color)
    }

    // Bolt fills the height; narrower when arcs flank it.
    let boltWidth: CGFloat = arcs ? 17 : 20
    let boltRect = CGRect(x: (canvas - boltWidth) / 2, y: 3, width: boltWidth, height: canvas - 6)
    for _ in 0..<max(glowPasses, 1) {
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
    context.restoreGState()
}

func render(color: CGColor, arcs: Bool, glow: Bool) -> CGImage {
    let context = makeContext()
    let canvas = CGFloat(size)
    drawGlyph(
        context, color: color, arcs: arcs,
        glowBlur: glow ? canvas * 0.22 : 0,
        // Double pass strengthens the halo.
        glowPasses: glow ? 2 : 1
    )
    return context.makeImage()!
}

func lerp(_ a: CGFloat, _ b: CGFloat, _ t: CGFloat) -> CGFloat { a + (b - a) * t }

func mix(_ a: CGColor, _ b: CGColor, _ t: CGFloat) -> CGColor {
    let ca = a.components!
    let cb = b.components!
    return CGColor(
        red: lerp(ca[0], cb[0], t), green: lerp(ca[1], cb[1], t),
        blue: lerp(ca[2], cb[2], t), alpha: 1
    )
}

/// One frame of the activity pulse. `intensity` (0...1) drives the
/// brightness/halo swell of the glyph; `ripple` (0...1) drives the ring
/// expanding from the bolt outward, fading to nothing before the edge.
func renderPulseFrame(base: CGColor, peak: CGColor, intensity: CGFloat, ripple: CGFloat) -> CGImage {
    let context = makeContext()
    let canvas = CGFloat(size)
    let center = CGPoint(x: canvas / 2, y: canvas / 2)
    let glyphColor = mix(base, peak, intensity)

    // Ring first: it emanates from under the bolt. It reuses the glyph
    // color (not a lighter tint): a pale ring vanishes on light menu bars.
    let ringAlpha = 0.9 * pow(1 - ripple, 1.5)
    if ringAlpha > 0.02 {
        context.saveGState()
        context.setStrokeColor(glyphColor.copy(alpha: ringAlpha)!)
        context.setLineWidth(3.4 - 1.8 * ripple)
        context.addArc(
            center: center, radius: canvas * (0.20 + 0.30 * ripple),
            startAngle: 0, endAngle: 2 * .pi, clockwise: false
        )
        context.strokePath()
        context.restoreGState()
    }

    drawGlyph(
        context, color: glyphColor, arcs: true,
        // The halo swells with the glyph: at 22 pt the expanding glow
        // carries much of the signal.
        glowBlur: canvas * (0.22 + 0.18 * intensity),
        glowPasses: intensity > 0.55 ? 3 : 2
    )
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
// Activity pulse peak: a vivid electric green. It must stay saturated —
// on light menu bars brightness cannot carry the signal (a whiter glyph
// only loses contrast there); saturation, the swelling halo, and the
// ripple ring do.
let pulsePeak = CGColor(red: 0.55, green: 1.00, blue: 0.45, alpha: 1)

write(render(color: green, arcs: true, glow: true), "tray-connected.png")
write(render(color: red, arcs: false, glow: false), "tray-disconnected.png")
write(render(color: black, arcs: true, glow: false), "tray-connected-template.png")
write(render(color: black, arcs: false, glow: false), "tray-disconnected-template.png")

for frame in 0..<pulseFrameCount {
    // t starts one step in (frame 0 already shows the attack, instead of
    // duplicating the resting icon) and lands exactly on 1 (resting look),
    // so the animation ends where the state icon takes over.
    let t = CGFloat(frame + 1) / CGFloat(pulseFrameCount)
    let attackEnd: CGFloat = 0.14
    let intensity: CGFloat
    if t < attackEnd {
        // Smoothstep attack: bright within ~2 frames of the request.
        let u = t / attackEnd
        intensity = u * u * (3 - 2 * u)
    } else {
        // Slow ease-out decay back to the resting glyph.
        let u = (t - attackEnd) / (1 - attackEnd)
        intensity = pow(1 - u, 1.8)
    }
    // Ease-out cubic: the ring shoots out fast, then drifts as it fades.
    let ripple = 1 - pow(1 - t, 3)
    write(
        renderPulseFrame(base: green, peak: pulsePeak, intensity: intensity, ripple: ripple),
        String(format: "tray-pulse-%02d.png", frame)
    )
}
print(outputDir.path)
