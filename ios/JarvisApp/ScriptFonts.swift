import JarvisClient
import SwiftUI

// Temporary: the fonts and looks the User can try in the conversation. Remove with ScriptStyle,
// the Settings section, the Fonts folder and UIAppFonts in project.yml.

struct ScriptFont: Identifiable {
    enum Face {
        /// Serif replies and system text for everything else, as the app shipped.
        case original
        case system(Font.Design)
        case family(String)
    }

    let id: String
    let name: String
    let face: Face

    /// Sizes arrive already scaled by `@ScaledMetric`, so bundled faces use a fixed size to avoid scaling twice.
    func font(size: CGFloat, reply: Bool = false) -> Font {
        switch face {
        case .original: .system(size: size, design: reply ? .serif : .default)
        case .system(let design): .system(size: size, design: design)
        case .family(let family): .custom(family, fixedSize: size).weight(.regular)
        }
    }

    static func with(id: String) -> ScriptFont { all.first { $0.id == id } ?? all[0] }

    static let all: [ScriptFont] = [
        ScriptFont(id: "original", name: "Original", face: .original),
        ScriptFont(id: "sf-mono", name: "SF Mono", face: .system(.monospaced)),
        ScriptFont(id: "menlo", name: "Menlo", face: .family("Menlo")),
        ScriptFont(id: "courier-new", name: "Courier New", face: .family("Courier New")),
        ScriptFont(id: "jetbrains-mono", name: "JetBrains Mono", face: .family("JetBrains Mono")),
        ScriptFont(id: "fira-code", name: "Fira Code", face: .family("Fira Code")),
        ScriptFont(id: "source-code-pro", name: "Source Code Pro", face: .family("Source Code Pro")),
        ScriptFont(id: "ibm-plex-mono", name: "IBM Plex Mono", face: .family("IBM Plex Mono")),
        ScriptFont(id: "hack", name: "Hack", face: .family("Hack")),
        ScriptFont(id: "inconsolata", name: "Inconsolata", face: .family("Inconsolata")),
        ScriptFont(id: "ubuntu-mono", name: "Ubuntu Mono", face: .family("Ubuntu Mono")),
        ScriptFont(id: "cascadia-code", name: "Cascadia Code", face: .family("Cascadia Code")),
        ScriptFont(id: "roboto-mono", name: "Roboto Mono", face: .family("Roboto Mono")),
        ScriptFont(id: "space-mono", name: "Space Mono", face: .family("Space Mono")),
        ScriptFont(id: "dejavu-sans-mono", name: "DejaVu Sans Mono", face: .family("DejaVu Sans Mono")),
        ScriptFont(id: "victor-mono", name: "Victor Mono", face: .family("Victor Mono")),
        ScriptFont(id: "geist-mono", name: "Geist Mono", face: .family("Geist Mono")),
        ScriptFont(id: "red-hat-mono", name: "Red Hat Mono", face: .family("Red Hat Mono")),
        ScriptFont(id: "martian-mono", name: "Martian Mono", face: .family("Martian Mono")),
        ScriptFont(id: "atkinson-hyperlegible-mono", name: "Atkinson Hyperlegible Mono",
                   face: .family("Atkinson Hyperlegible Mono")),
        ScriptFont(id: "intel-one-mono", name: "Intel One Mono", face: .family("Intel One Mono")),
        ScriptFont(id: "commit-mono", name: "Commit Mono", face: .family("CommitMono")),
        ScriptFont(id: "anonymous-pro", name: "Anonymous Pro", face: .family("Anonymous Pro")),
        ScriptFont(id: "share-tech-mono", name: "Share Tech Mono", face: .family("Share Tech Mono")),
        ScriptFont(id: "vt323", name: "VT323", face: .family("VT323"))
    ]
}

extension ScriptStyle.Look {
    var name: String {
        switch self {
        case .current: "Current"
        case .terminal: "Terminal"
        case .gutter: "Gutter"
        }
    }

    var summary: String {
        switch self {
        case .current: "Your lines in bubbles, Jarvis in full-width text."
        case .terminal: "A shell transcript: every line starts at the left under its speaker and time."
        case .gutter: "Times in a left margin, your lines as shaded blocks, Details in a framed panel."
        }
    }
}
