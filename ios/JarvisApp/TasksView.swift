import JarvisClient
import SwiftUI

struct TasksView: View {
    @Bindable var session: AppSession
    @ScaledMetric(relativeTo: .subheadline) var controlSize = 15
    private var theme: JarvisTheme { JarvisTheme() }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            ScrollView {
                TimelineView(.periodic(from: .now, by: 30)) { _ in
                    VStack(alignment: .leading, spacing: 32) {
                        if session.updatingTasks {
                            Text("Updating…").font(.system(size: controlSize))
                        } else if !session.connection.canSend {
                            Text(session.connection.message).font(.system(size: controlSize))
                        }
                        if session.openTasks.isEmpty && !session.updatingTasks {
                            Text("No open Tasks. Ask Jarvis for something.")
                                .font(.system(size: controlSize))
                        }
                        taskSection("Waiting for you", waiting: true)
                        taskSection("Running", waiting: false)
                    }
                    .foregroundStyle(session.updatingTasks ? theme.haze : theme.frost)
                    .frame(maxWidth: 640, alignment: .leading)
                    .frame(maxWidth: .infinity, alignment: .center)
                    .padding(20)
                }
            }
        }
        .background(theme.void)
    }

    @ViewBuilder private func taskSection(_ title: String, waiting: Bool) -> some View {
        let tasks = session.openTasks.filter { $0.waitingForUser == waiting }
        if !tasks.isEmpty {
            VStack(alignment: .leading, spacing: 24) {
                Text(title).font(.system(size: controlSize)).foregroundStyle(theme.haze)
                    .accessibilityAddTraits(.isHeader)
                ForEach(tasks) { task in
                    VStack(alignment: .leading, spacing: 8) {
                        if let question = task.approvalQuestion, !question.isEmpty {
                            Text(verbatim: question).font(.system(size: controlSize))
                                .lineSpacing(5).textSelection(.enabled)
                        }
                        Text(verbatim: task.title).font(.system(size: controlSize)).textSelection(.enabled)
                        if let report = task.lastReport, !report.isEmpty {
                            Text(verbatim: report).font(.system(size: controlSize)).foregroundStyle(theme.haze)
                                .textSelection(.enabled)
                        }
                    }
                }
            }
        }
    }
}

struct WorkspaceView: View {
    @Bindable var session: AppSession
    @ScaledMetric(relativeTo: .subheadline) private var controlSize = 15
    @ScaledMetric(relativeTo: .body) private var scriptSize = 18
    @State private var expanded = false
    @State private var keyboardVisible = false
    private let theme = JarvisTheme()

    var body: some View {
        VStack(spacing: 0) {
            WorkspaceTopBar(session: session, controlSize: controlSize)
            GeometryReader { geometry in
                let minimumConversation = 3 * (scriptSize * 1.25 + 5) + 40
                let automaticallyExpanded = keyboardVisible && geometry.size.height < 120 + minimumConversation + 44
                let showingExpanded = expanded || automaticallyExpanded
                let orbHeight = keyboardVisible
                    ? max(120, min(geometry.size.height * 0.4, geometry.size.height - minimumConversation - 44))
                    : geometry.size.height * 0.4
                VStack(spacing: 0) {
                    OrbArea(session: session, controlSize: controlSize, isVisible: !showingExpanded)
                        .frame(height: showingExpanded ? 0 : orbHeight)
                        .opacity(showingExpanded ? 0 : 1)
                        .clipped()
                        .allowsHitTesting(!showingExpanded)
                        .accessibilityHidden(showingExpanded)
                    expansionControl(showingExpanded: showingExpanded)
                    ConversationView(session: session, showsInputBar: false)
                        .overlay(alignment: .top) {
                            // An overlay rather than a mask, so the script can still scroll behind the composer.
                            LinearGradient(colors: [theme.void, theme.void.opacity(0)], startPoint: .top, endPoint: .bottom)
                                .frame(height: showingExpanded ? 0 : 24)
                                .allowsHitTesting(false)
                        }
                }
                .animation(.easeInOut(duration: 0.25), value: showingExpanded)
            }
        }
        .background(theme.void)
        .safeAreaInset(edge: .bottom, spacing: 0) {
            MessageInputBar(session: session)
        }
        #if os(iOS)
        .toolbar(.hidden, for: .navigationBar)
        .onReceive(NotificationCenter.default.publisher(for: UIResponder.keyboardWillShowNotification)) { _ in
            keyboardVisible = true
        }
        .onReceive(NotificationCenter.default.publisher(for: UIResponder.keyboardWillHideNotification)) { _ in
            keyboardVisible = false
        }
        #endif
    }

    private func expansionControl(showingExpanded: Bool) -> some View {
        Button {
            setExpanded(!showingExpanded)
        } label: {
            BendingChevron(bend: showingExpanded ? -1 : 1)
                .stroke(style: StrokeStyle(lineWidth: 2, lineCap: .round, lineJoin: .round))
                .frame(width: 22, height: 6)
                .frame(maxWidth: .infinity, minHeight: 44)
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .foregroundStyle(theme.haze)
        .accessibilityLabel("Conversation")
        .accessibilityValue(showingExpanded ? "Expanded" : "Collapsed")
        .accessibilityHint(showingExpanded ? "Collapse the conversation" : "Expand the conversation")
    }

    private func setExpanded(_ value: Bool) {
        if !value, keyboardVisible { dismissKeyboard() }
        withAnimation(.easeInOut(duration: 0.25)) { expanded = value }
    }
}

/// A chevron that points up at bend 1 and down at bend -1, passing through a flat line between them.
private struct BendingChevron: Shape {
    var bend: CGFloat
    var animatableData: CGFloat {
        get { bend }
        set { bend = newValue }
    }

    func path(in rect: CGRect) -> Path {
        let rise = rect.height / 2 * bend
        var path = Path()
        path.move(to: CGPoint(x: rect.minX, y: rect.midY + rise))
        path.addLine(to: CGPoint(x: rect.midX, y: rect.midY - rise))
        path.addLine(to: CGPoint(x: rect.maxX, y: rect.midY + rise))
        return path
    }
}

private func dismissKeyboard() {
    #if os(iOS)
    UIApplication.shared.sendAction(#selector(UIResponder.resignFirstResponder), to: nil, from: nil, for: nil)
    #endif
}

struct OrbArea: View {
    @Bindable var session: AppSession
    var controlSize: CGFloat
    var isVisible = true

    var body: some View {
        PointOrbView(level: session.orb.level, speaker: session.orb.speaker, isVisible: isVisible)
            .contentShape(Rectangle())
            .onTapGesture(perform: dismissKeyboard)
            .accessibilityHidden(true)
            .overlay(alignment: .bottomTrailing) {
                if isVisible {
                    VoiceControl(session: session, controlSize: controlSize)
                        .padding(.trailing, 20)
                        .padding(.bottom, 12)
                }
            }
    }
}

struct WorkspaceTopBar: View {
    @Bindable var session: AppSession
    var controlSize: CGFloat
    @State private var showingConnection = false
    private let theme = JarvisTheme()

    var body: some View {
        ViewThatFits(in: .horizontal) {
            HStack(spacing: 12) {
                settingsLink
                Spacer(minLength: 0)
                connectionButton(showsLabel: true)
                Spacer(minLength: 0)
                tasksLink
            }
            VStack(spacing: 0) {
                HStack(spacing: 12) {
                    settingsLink
                    Spacer(minLength: 0)
                    connectionButton(showsLabel: false)
                    Spacer(minLength: 0)
                    tasksLink
                }
                if let label = session.shownConnection.indicator.label {
                    Text(label).multilineTextAlignment(.center)
                        .fixedSize(horizontal: false, vertical: true)
                        .accessibilityHidden(true)
                }
            }
        }
        .buttonStyle(.plain)
        .font(.system(size: controlSize))
        .foregroundStyle(theme.haze)
        .padding(.horizontal, 20)
        .background(theme.void)
        .alert("Connection status", isPresented: $showingConnection) {
            Button("OK", role: .cancel) {}
        } message: { Text(session.shownConnection.message) }
    }

    private var settingsLink: some View {
        NavigationLink {
            SettingsView(session: session)
                .navigationTitle("Settings")
                #if os(iOS)
                .navigationBarTitleDisplayMode(.inline)
                .toolbar(.visible, for: .navigationBar)
                #endif
        } label: {
            Image(systemName: "gearshape").frame(minWidth: 44, minHeight: 44)
                .contentShape(Rectangle())
        }
        .accessibilityLabel("Settings")
        .fixedSize()
    }

    private func connectionButton(showsLabel: Bool) -> some View {
        Button { showingConnection = true } label: {
            HStack(spacing: 8) {
                Circle().fill(connectionColor).frame(width: 8, height: 8)
                if showsLabel, let label = session.shownConnection.indicator.label { Text(label) }
            }
            .frame(minWidth: 44, minHeight: 44)
            .contentShape(Rectangle())
        }
        .accessibilityLabel("Connection status")
        .accessibilityValue(session.shownConnection.message)
        .accessibilityHint("Show connection details")
        .fixedSize()
    }

    private var tasksLink: some View {
        NavigationLink {
            TasksView(session: session)
                .navigationTitle("Tasks")
                #if os(iOS)
                .navigationBarTitleDisplayMode(.inline)
                .toolbar(.visible, for: .navigationBar)
                #endif
                .safeAreaInset(edge: .bottom, spacing: 0) {
                    MessageInputBar(session: session)
                }
        } label: {
            HStack(spacing: 5) {
                Image(systemName: "checklist")
                Text(session.openTasks.count, format: .number)
            }
            .frame(minWidth: 44, minHeight: 44)
            .contentShape(Rectangle())
        }
        .accessibilityLabel("Tasks")
        .accessibilityValue("\(session.openTasks.count) open")
        .fixedSize()
    }

    private var connectionColor: Color {
        switch session.shownConnection.indicator.tone {
        case .green: theme.green
        case .amber: theme.amber
        case .red: theme.signal
        }
    }
}
