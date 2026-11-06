import Foundation
import Speech
import AVFAudio
import Darwin

func emit(_ value: [String: Any]) throws {
    let data = try JSONSerialization.data(withJSONObject: value, options: [.sortedKeys])
    print(String(decoding: data, as: UTF8.self))
    fflush(stdout)
}

@main struct AppleWake {
    static func main() async {
        do {
            let argument = CommandLine.arguments[1]
            guard SpeechTranscriber.isAvailable else {
                throw NSError(domain: "AppleWake", code: 3, userInfo: [NSLocalizedDescriptionKey: "SpeechTranscriber is unavailable on this device; select openwakeword."])
            }
            let module = SpeechTranscriber(locale: Locale(identifier: "en_US"), preset: .transcription)
            if argument == "--install", await AssetInventory.status(forModules: [module]) != .installed {
                let locales = await AssetInventory.reservedLocales
                guard locales.contains(where: { $0.identifier == "en_US" }) || locales.count < AssetInventory.maximumReservedLocales else {
                    throw NSError(domain: "AppleWake", code: 1, userInfo: [NSLocalizedDescriptionKey: "Language reservation capacity reached; no languages were evicted."])
                }
                if let request = try await AssetInventory.assetInstallationRequest(supporting: [module]) {
                    try await request.downloadAndInstall()
                }
            }
            guard await AssetInventory.status(forModules: [module]) == .installed else {
                throw NSError(domain: "AppleWake", code: 2, userInfo: [NSLocalizedDescriptionKey: "English (US) assets missing; run the explicit --install setup command."])
            }
            if argument == "--check" || argument == "--install" {
                try emit(["ready": true, "locale": "en_US"])
                return
            }
            let analyzer = SpeechAnalyzer(modules: [module], options: .init(priority: .utility, modelRetention: .whileInUse))
            let reader = Task { () throws -> [String] in
                var texts: [String] = []
                for try await result in module.results where result.isFinal {
                    texts.append(String(result.text.characters))
                }
                return texts
            }
            do {
                let file = try AVAudioFile(forReading: URL(fileURLWithPath: argument))
                if let last = try await analyzer.analyzeSequence(from: file) {
                    try await analyzer.finalizeAndFinish(through: last)
                } else {
                    await analyzer.cancelAndFinishNow()
                }
                try emit(["segments": try await reader.value])
            } catch {
                await analyzer.cancelAndFinishNow()
                reader.cancel()
                _ = try? await reader.value
                throw error
            }
            await SpeechModels.endRetention()
        } catch {
            try? emit(["error": String(describing: error)])
            exit(1)
        }
    }
}
