import JarvisClient
import SwiftUI

struct ConversationView: View {
    @Bindable var session: AppSession
    var showsTalkControls = true
    var openTasks: (() -> Void)? = nil
    var typingChanged: (Bool) -> Void = { _ in }
    @Environment(\.colorScheme) private var scheme
    @ScaledMetric(relativeTo: .body) var scriptSize = 19
    @ScaledMetric(relativeTo: .subheadline) var controlSize = 15
    @FocusState private var typing: Bool
    @State private var followsLatest = true
    @State private var isAtBottom = true
    @State private var detailsChanging = false
    @State private var detailsRevision = 0
    @State private var readingAnchor: String?
    @State private var scrollPhase: ScrollPhase = .idle
    @State private var recoveryCandidate: String?
    @State var expandedDetails: Set<String> = []
    private var theme: JarvisTheme { JarvisTheme(scheme: scheme) }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(alignment: .firstTextBaseline, spacing: 12) {
                Text(session.connection.message).accessibilityAddTraits(.isHeader)
                Spacer(minLength: 0)
                if let openTasks {
                    Button(action: openTasks) {
                        Text("Tasks \(session.openTasks.count)").underline().frame(minHeight: 44)
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("Tasks, \(session.openTasks.count) open")
                }
                NavigationLink { SettingsView(session: session) } label: {
                    Text("Settings").frame(minWidth: 44, minHeight: 44).contentShape(Rectangle())
                }
            }
            .font(.system(size: controlSize))
            .foregroundStyle(theme.secondary)
            .padding(20)
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 32) {
                    if session.hasOlderHistory {
                        VStack(alignment: .leading) {
                            if session.loadingHistory { ProgressView("Loading older history…") }
                            else if session.connection.canSend {
                                Button { Task { await session.loadOlderHistory() } } label: {
                                    Text("Older history").underline()
                                        .frame(minWidth: 44, minHeight: 44, alignment: .leading)
                                        .contentShape(Rectangle())
                                }
                                .buttonStyle(.plain)
                            } else { Text("Older history") }
                        }
                        .font(.system(size: controlSize))
                        .foregroundStyle(theme.secondary).frame(minHeight: 44)
                        .task(id: session.connection.canSend) { await session.loadOlderHistory() }
                    }
                    if let message = session.historyMessage {
                        Text(message).font(.system(size: controlSize)).foregroundStyle(theme.secondary)
                    }
                    if session.conversation.rows.isEmpty {
                        Text("Nothing said yet. Hold the key and ask Jarvis something.")
                            .font(.system(size: scriptSize, design: .serif))
                            .lineSpacing(5)
                            .padding(.top, 24)
                    }
                    ForEach(Array(session.conversation.rows.enumerated()), id: \.element.id) { index, row in
                        VStack(alignment: .leading, spacing: 12) {
                            if let date = gapDate(index) {
                                Text(date, format: .dateTime.month(.abbreviated).day().hour().minute())
                                    .font(.system(size: controlSize))
                                    .foregroundStyle(theme.secondary)
                            }
                            scriptRow(row)
                        }.id(row.id)
                    }
                    ForEach(session.typedLines.filter { !$0.message.isEmpty }) { line in
                        typedLineStatus(line)
                    }
                }
                .scrollTargetLayout()
                .foregroundStyle(theme.text)
                .frame(maxWidth: 640, alignment: .leading)
                .frame(maxWidth: .infinity)
                .padding(20)
            }
            .scrollPosition(id: $readingAnchor, anchor: .top)
            .defaultScrollAnchor(.bottom, for: .initialOffset)
            .defaultScrollAnchor(followsLatest ? .bottom : nil, for: .sizeChanges)
            .defaultScrollAnchor(.top, for: .alignment)
            .onScrollGeometryChange(for: Bool.self) { geometry in
                geometry.contentSize.height - geometry.visibleRect.maxY <= 60
            } action: { _, atBottom in
                isAtBottom = atBottom
                // Streamed layout changes cannot reclaim a reader who has scrolled up.
                if !detailsChanging && scrollPhase != .idle && scrollPhase != .animating { followsLatest = atBottom }
            }
            .onScrollPhaseChange { _, phase, context in
                scrollPhase = phase
                isAtBottom = context.geometry.contentSize.height - context.geometry.visibleRect.maxY <= 60
                if phase == .idle && !detailsChanging { followsLatest = isAtBottom }
            }
        }
        .background(theme.background)
        .onChange(of: typing) { typingChanged(typing) }
        .onDisappear { typingChanged(false) }
        .safeAreaInset(edge: .bottom, spacing: 0) { composer }
        .confirmationDialog("Restore as a new line?", isPresented: Binding(
            get: { recoveryCandidate != nil }, set: { if !$0 { recoveryCandidate = nil } }
        ), titleVisibility: .visible) {
            Button("I've checked: restore line") {
                if let id = recoveryCandidate { session.recoverTypedLine(id: id) }
                recoveryCandidate = nil
            }
            Button("Cancel", role: .cancel) { recoveryCandidate = nil }
        } message: {
            Text("Check the conversation first. This line may already have reached Jarvis. Sending it again can repeat it. Restoring only puts it in the typing field.")
        }
    }

    private func typedLineStatus(_ line: TypedLine) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(line.message)
            if line.canRecover {
                Text(verbatim: line.text).textSelection(.enabled)
                Button {
                    if line.outcome == .outcomeUnknown { recoveryCandidate = line.id }
                    else { session.recoverTypedLine(id: line.id) }
                } label: {
                    Text(line.outcome == .outcomeUnknown ? "Review and restore line" : "Restore line")
                        .underline()
                        .frame(minHeight: 44)
                }
                .buttonStyle(.plain)
                .disabled(!session.draft.isEmpty && session.draft != line.text)
                if !session.draft.isEmpty && session.draft != line.text {
                    Text("Finish or clear your current draft to restore this line. You can also copy the text above.")
                }
            }
        }
        .font(.system(size: controlSize))
        .foregroundStyle(theme.secondary)
    }

    private func gapDate(_ index: Int) -> Date? {
        let rows = session.conversation.rows
        guard index > 0, let current = utcDate(rows[index].time),
              let previous = utcDate(rows[index - 1].time), current.timeIntervalSince(previous) > 3600 else { return nil }
        return current
    }

    private func scriptRow(_ row: ConversationRow) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            if !row.text.isEmpty {
                Text(spokenText(row))
                    .font(.system(size: row.speaker == .user ? controlSize : scriptSize,
                                  design: row.speaker == .user ? .default : .serif))
                    .lineSpacing(row.speaker == .user ? 2 : 5)
                    .foregroundStyle(row.speaker == .user ? theme.secondary : theme.text)
                    .multilineTextAlignment(row.speaker == .user ? .trailing : .leading)
                    .frame(maxWidth: .infinity, alignment: row.speaker == .user ? .trailing : .leading)
                    .textSelection(.enabled)
            }
            if row.id.hasPrefix("reply:"), session.playback.missedReplies.contains(String(row.id.dropFirst(6))),
               session.playback.replyID != String(row.id.dropFirst(6)) {
                Button("Play") { Task { await session.replay(replyID: String(row.id.dropFirst(6))) } }
                    .buttonStyle(.plain)
                    .font(.system(size: controlSize))
                    .foregroundStyle(theme.secondary)
                    .underline()
                    .frame(minWidth: 44, minHeight: 44)
                    .contentShape(Rectangle())
                    .disabled(!session.voiceOn || !session.connection.canSend)
                    .accessibilityLabel("Play reply")
                if let message = session.replayMessages[String(row.id.dropFirst(6))] ?? session.playback.messages[String(row.id.dropFirst(6))] {
                    Text(message).font(.system(size: controlSize)).foregroundStyle(theme.secondary)
                }
            }
            if !row.details.isEmpty {
                DisclosureGroup(isExpanded: Binding(get: { expandedDetails.contains(row.id) }, set: { expanded in
                    guard expanded != expandedDetails.contains(row.id) else { return }
                    detailsRevision += 1
                    let revision = detailsRevision
                    detailsChanging = true
                    followsLatest = false
                    withAnimation(.default, completionCriteria: .removed) {
                        if expanded { expandedDetails.insert(row.id) } else { expandedDetails.remove(row.id) }
                    } completion: {
                        guard revision == detailsRevision else { return }
                        detailsChanging = false
                        if scrollPhase == .idle { followsLatest = isAtBottom }
                    }
                })) {
                    ForEach(row.details) { detail in
                        VStack(alignment: .leading, spacing: 8) {
                            Text(verbatim: detail.title).fontWeight(.semibold)
                            Text(verbatim: detail.text).textSelection(.enabled)
                        }
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(.vertical, 8)
                    }
                } label: { Text("Details").frame(minHeight: 44) }
                .font(.system(size: controlSize))
                .foregroundStyle(theme.secondary)
                .tint(theme.secondary)
            }
        }
    }

    private func spokenText(_ row: ConversationRow) -> AttributedString {
        guard row.id == session.playback.replyID.map({ "reply:\($0)" }),
              let sentence = session.playback.sentence else { return AttributedString(row.text) }
        var cursor = row.text.startIndex
        for completed in session.playback.completedSentences {
            if let range = row.text.range(of: completed, range: cursor..<row.text.endIndex) { cursor = range.upperBound }
        }
        guard let range = row.text.range(of: sentence, range: cursor..<row.text.endIndex) else { return AttributedString(row.text) }
        var marked = AttributedString(row.text[range])
        marked.underlineStyle = .single
        marked.font = .system(size: scriptSize, design: .serif).bold()
        return AttributedString(row.text[..<range.lowerBound]) + marked + AttributedString(row.text[range.upperBound...])
    }

    private var composer: some View {
        VStack(spacing: 12) {
            if let message = session.draftValidationMessage ?? session.inputMessage {
                Text(message).foregroundStyle(theme.secondary)
            }
            HStack(alignment: .bottom, spacing: 8) {
                TextField("Type instead", text: $session.draft, axis: .vertical)
                    .lineLimit(1...5)
                    .textFieldStyle(.plain)
                    .focused($typing)
                    .disabled(!session.connection.canSend)
                    .frame(minHeight: 44)
                    .onSubmit { submit() }
                Button(action: submit) {
                    Image(systemName: "arrow.up").frame(width: 44, height: 44)
                }
                .accessibilityLabel("Send line")
                .buttonStyle(.plain)
                .disabled(!session.canSubmit)
            }
            .padding(.horizontal, 16)
            .padding(.vertical, 4)
            .foregroundStyle(theme.secondary)
            .overlay(RoundedRectangle(cornerRadius: 8).stroke(theme.secondary, lineWidth: 1))

            if showsTalkControls { TalkControls(session: session, controlSize: controlSize, showsKeys: !typing) }
        }
        .font(.system(size: controlSize))
        .padding(20)
        .background(theme.background)
    }

    private func submit() { Task { await session.sendTypedLine() } }
}

struct VoiceControl: View {
    @Bindable var session: AppSession
    var controlSize: CGFloat
    @Environment(\.colorScheme) private var scheme
    private var theme: JarvisTheme { JarvisTheme(scheme: scheme) }

    var body: some View {
        Button { Task { await session.setVoiceOn(!session.voiceOn) } } label: {
            Text(session.voiceOn ? "Voice on" : "Voice off")
                .font(.system(size: controlSize))
                .multilineTextAlignment(.center)
                .padding(12)
                .frame(minWidth: 72, minHeight: controlSize > 20 ? 44 : 88)
                .frame(maxWidth: controlSize > 20 ? .infinity : nil)
                .contentShape(Rectangle())
                .overlay(RoundedRectangle(cornerRadius: 10).stroke(theme.secondary, lineWidth: 1))
        }
        .buttonStyle(.plain)
        .foregroundStyle(theme.secondary)
        .accessibilityHint(session.voiceOn ? "Turn off speech on this phone" : "Turn on speech on this phone")
        .accessibilityValue(session.voicePending ? "Waiting for the Mac" : session.voiceOn ? "On" : "Off")
    }
}
