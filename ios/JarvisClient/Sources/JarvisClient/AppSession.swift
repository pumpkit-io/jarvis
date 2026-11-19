import Foundation
import Observation

@MainActor @Observable public final class AppSession {
    public private(set) var credentials: PairingCredentials?
    public private(set) var connection: HubConnectionState = .unpaired
    public private(set) var loading = true
    public private(set) var pairing = false
    public private(set) var error: PairingError?
    public private(set) var client: HubClient?
    private let store: any PairingStore
    private let service: PairingService
    private let makeClient: (PairingCredentials) throws -> HubClient
    private let makePaths: () -> HubNetworkPaths?
    private var paths: HubNetworkPaths?
    private var updates: Task<Void, Never>?
    private var generation: UInt64 = 0
    private var foreground = true
    private var launched = false

    public init(store: any PairingStore, service: PairingService = .urlSession,
                makeClient: @escaping (PairingCredentials) throws -> HubClient = {
                    HubClient(transport: try .urlSession(origin: $0.origin, token: $0.deviceToken))
                }, makePaths: @escaping () -> HubNetworkPaths? = { HubNetworkPaths() }) {
        self.store = store; self.service = service; self.makeClient = makeClient; self.makePaths = makePaths
    }

    public func launch() async {
        guard !launched else { return }
        launched = true
        let owner = generation
        do {
            let saved = try store.load()
            loading = false
            if let saved, generation == owner { try await connect(saved, owner: owner) }
        } catch { loading = false; self.error = .storage }
    }

    public func pair(_ input: PairingInput) async {
        guard !pairing, credentials == nil else { return }
        generation &+= 1
        let owner = generation
        pairing = true; error = nil
        do {
            let result = try await service.pair(input)
            guard generation == owner else { return }
            do { try store.save(result) } catch { throw PairingError.storage }
            pairing = false
            try await connect(result, owner: owner)
        } catch {
            guard generation == owner else { return }
            pairing = false
            if !(error is CancellationError) { self.error = error as? PairingError ?? .invalidResponse }
        }
    }

    public func showError(_ error: PairingError) { self.error = error }

    /// Invalidates in-flight pairing before clearing the sole durable credential record.
    public func unpair() async {
        generation &+= 1
        let old = client
        updates?.cancel(); updates = nil; paths?.cancel(); paths = nil
        client = nil; credentials = nil; connection = .unpaired; pairing = false; loading = false
        do { try store.clear(); error = nil } catch { self.error = .storage }
        await old?.stop()
    }

    public func setForeground(_ value: Bool) async {
        foreground = value
        await client?.setForeground(value)
    }

    private func connect(_ credentials: PairingCredentials, owner: UInt64) async throws {
        guard generation == owner else { return }
        let client = try makeClient(credentials)
        self.credentials = credentials; self.client = client; connection = .connecting
        paths = makePaths()
        updates = Task { [weak self] in
            for await update in client.updates {
                guard !Task.isCancelled, let self, self.generation == owner else { return }
                if case .state(let state) = update {
                    if state == .unpaired { await self.unpair(); return }
                    self.connection = state
                }
            }
        }
        if !foreground { await client.setForeground(false) }
        guard generation == owner else { await client.stop(); return }
        await client.start(paths: paths?.changes)
        if generation != owner { await client.stop() }
    }
}

public enum CameraPermission: Sendable { case unknown, allowed, refused }

@MainActor @Observable public final class PairingCamera {
    public private(set) var permission: CameraPermission
    private let request: @Sendable () async -> Bool
    public init(permission: CameraPermission = .unknown, request: @escaping @Sendable () async -> Bool) {
        self.permission = permission; self.request = request
    }
    public func authorize() async -> Bool {
        if permission == .unknown { permission = await request() ? .allowed : .refused }
        return permission == .allowed
    }
    public func refresh(_ permission: CameraPermission) { self.permission = permission }
}
