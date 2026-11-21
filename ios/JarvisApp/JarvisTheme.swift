import SwiftUI

struct JarvisTheme {
    let void = Color(hex: 0x05070C)
    let deep = Color(hex: 0x0E1420)
    let frost = Color(hex: 0xE6EEF5)
    let haze = Color(hex: 0x7F8EA3)
    let cyan = Color(hex: 0x33E6FF)
    let ice = Color(hex: 0xD9EDFF)
    let idleBlue = Color(hex: 0x5AB4FF)
    let signal = Color(hex: 0xD6402E)
    let green = Color(hex: 0x3DDC84)
    let amber = Color(hex: 0xF5B544)
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
