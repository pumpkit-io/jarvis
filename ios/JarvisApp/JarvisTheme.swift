import SwiftUI

struct JarvisTheme {
    let scheme: ColorScheme

    var background: Color { scheme == .dark ? Self.baize : Self.linen }
    var text: Color { scheme == .dark ? Self.linen : Self.baize }
    var secondary: Color { Color(hex: scheme == .dark ? 0x8FA39C : 0x536D64) }
    var brass: Color { Color(hex: scheme == .dark ? 0xC39A4A : 0x806020) }
    var signal: Color { Color(hex: 0xD6402E) }
    var keyText: Color { scheme == .dark ? Self.baize : Self.linen }

    static let baize = Color(hex: 0x16302B)
    static let linen = Color(hex: 0xE9EEEA)
}

private extension Color {
    init(hex: UInt32) {
        self.init(
            .sRGB,
            red: Double((hex >> 16) & 0xFF) / 255,
            green: Double((hex >> 8) & 0xFF) / 255,
            blue: Double(hex & 0xFF) / 255,
            opacity: 1
        )
    }
}
