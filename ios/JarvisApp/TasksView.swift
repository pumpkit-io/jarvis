import JarvisClient
import SwiftUI

struct TasksView: View {
    @Bindable var session: AppSession
    var conversation: () -> Void = {}
    @ScaledMetric(relativeTo: .subheadline) var controlSize = 15
    private var theme: JarvisTheme { JarvisTheme() }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack {
                Button(action: conversation) { Text("Conversation").underline().frame(minHeight: 44) }
                    .buttonStyle(.plain)
                Spacer()
                Text("Tasks").accessibilityAddTraits(.isHeader)
            }
            .font(.system(size: controlSize))
            .foregroundStyle(theme.haze)
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
    @State private var showingTasks = false
    @State private var typing = false
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
                    .padding(20).background(JarvisTheme().void)
            }
        }
    }
}
