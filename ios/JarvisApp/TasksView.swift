import JarvisClient
import SwiftUI

struct TasksView: View {
    @Bindable var session: AppSession
    var conversation: () -> Void = {}
    @Environment(\.colorScheme) private var scheme
    @ScaledMetric(relativeTo: .body) var scriptSize = 19
    @ScaledMetric(relativeTo: .subheadline) var controlSize = 15
    private var theme: JarvisTheme { JarvisTheme(scheme: scheme) }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack {
                Button(action: conversation) { Text("Conversation").underline().frame(minHeight: 44) }
                    .buttonStyle(.plain)
                Spacer()
                Text("Tasks").accessibilityAddTraits(.isHeader)
            }
            .font(.system(size: controlSize))
            .foregroundStyle(theme.secondary)
            .padding(.horizontal, 20)
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
                                .font(.system(size: scriptSize, design: .serif))
                        }
                        taskSection("Waiting for you", waiting: true)
                        taskSection("Running", waiting: false)
                    }
                    .foregroundStyle(session.updatingTasks ? theme.secondary : theme.text)
                    .frame(maxWidth: 640, alignment: .leading)
                    .frame(maxWidth: .infinity, alignment: .center)
                    .padding(20)
                }
            }
        }
        .background(theme.background)
    }

    @ViewBuilder private func taskSection(_ title: String, waiting: Bool) -> some View {
        let tasks = session.openTasks.filter { $0.waitingForUser == waiting }
        if !tasks.isEmpty {
            VStack(alignment: .leading, spacing: 24) {
                Text(title).font(.system(size: controlSize)).foregroundStyle(theme.secondary)
                    .accessibilityAddTraits(.isHeader)
                ForEach(tasks) { task in
                    VStack(alignment: .leading, spacing: 8) {
                        if let question = task.approvalQuestion, !question.isEmpty {
                            Text(verbatim: question).font(.system(size: scriptSize, design: .serif))
                                .lineSpacing(5).textSelection(.enabled)
                        }
                        Text(verbatim: task.title).font(.system(size: controlSize)).textSelection(.enabled)
                        if let report = task.lastReport, !report.isEmpty {
                            Text(verbatim: report).font(.system(size: controlSize)).foregroundStyle(theme.secondary)
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
    @State private var showingTasks = false
    @State private var typing = false
    @Environment(\.colorScheme) private var scheme
    @ScaledMetric(relativeTo: .subheadline) private var controlSize = 15

    var body: some View {
        Group {
            if showingTasks { TasksView(session: session, conversation: { showingTasks = false }) }
            else {
                ConversationView(session: session, showsTalkControls: false,
                                 openTasks: { typing = false; showingTasks = true }, typingChanged: { typing = $0 })
            }
        }
        .safeAreaInset(edge: .bottom, spacing: 0) {
            if !typing || session.voiceMessage != nil || session.talk.message != nil {
                TalkControls(session: session, controlSize: controlSize, showsKeys: !typing)
                    .padding(20).background(JarvisTheme(scheme: scheme).background)
            }
        }
    }
}
