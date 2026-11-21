import JarvisClient
import SwiftUI

struct ConversationView: View {
    @Bindable var session: AppSession
    var showsInputBar = true
    var typingChanged: (Bool) -> Void = { _ in }
    @ScaledMetric(relativeTo: .body) var scriptSize = 18
    @ScaledMetric(relativeTo: .subheadline) var controlSize = 15
    @State private var followsLatest = true
    @State private var isAtBottom = true
    @State private var detailsChanging = false
    @State private var detailsRevision = 0
    @State private var readingAnchor: String?
    @State private var scrollPhase: ScrollPhase = .idle
    @State var expandedDetails: Set<String> = []
    private var theme: JarvisTheme { JarvisTheme() }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
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
                        .foregroundStyle(theme.haze).frame(minHeight: 44)
                        .task(id: session.connection.canSend) { await session.loadOlderHistory() }
                    }
                    if let message = session.historyMessage {
                        Text(message).font(.system(size: controlSize)).foregroundStyle(theme.haze)
                    }
                    if session.conversation.rows.isEmpty && (session.outbox?.items.isEmpty ?? true) {
                        Text("Ask Jarvis anything.")
                            .font(.system(size: controlSize))
                            .foregroundStyle(theme.haze)
                            .lineSpacing(5)
                            .padding(.top, 24)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                    ForEach(Array(session.conversation.rows.enumerated()), id: \.element.id) { index, row in
                        VStack(alignment: .leading, spacing: 12) {
                            if let date = gapDate(index) {
                                Text(date, format: .dateTime.month(.abbreviated).day().hour().minute())
                                    .font(.system(size: controlSize))
                                    .foregroundStyle(theme.haze)
                            }
                            scriptRow(row)
                        }
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .id(row.id)
                    }
                    ForEach(session.outbox?.items ?? []) { item in
                        queued(item).id("outbox:\(item.id)")
                    }
                }
                .scrollTargetLayout()
                .foregroundStyle(theme.frost)
                .frame(minWidth: 0, maxWidth: 640, alignment: .leading)
                .padding(20)
                .containerRelativeFrame(.horizontal)
            }
            // Leading anchors and a container-wide column leave no horizontal offset for a resize to introduce.
            .scrollPosition(id: $readingAnchor, anchor: .topLeading)
            .defaultScrollAnchor(.bottomLeading, for: .initialOffset)
            .defaultScrollAnchor(followsLatest ? .bottomLeading : nil, for: .sizeChanges)
            .defaultScrollAnchor(.topLeading, for: .alignment)
            .scrollBounceBehavior(.basedOnSize, axes: .horizontal)
            .scrollBounceBehavior(.always, axes: .vertical)
            .scrollDismissesKeyboard(.interactively)
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
        .background(theme.void)
        .safeAreaInset(edge: .bottom, spacing: 0) {
            if showsInputBar { MessageInputBar(session: session, typingChanged: typingChanged) }
        }
    }

    /// A message still on this phone, shown where the User's line will appear once Jarvis hears it.
    private func queued(_ item: OutboxItem) -> some View {
        userBubble {
            VStack(alignment: .leading, spacing: 8) {
                switch item.content {
                case .line(let text): Text(verbatim: text).textSelection(.enabled)
                case .talk(_, let seconds):
                    Label("\(Int(seconds.rounded())) s", systemImage: "waveform")
                        .accessibilityLabel("Talk, \(Int(seconds.rounded())) seconds")
                }
                Label(item.message, systemImage: "clock")
                    .foregroundStyle(theme.haze)
                HStack(spacing: 16) {
                    if item.isSettled {
                        Button(item.status == .uncertain ? "Send again" : "Try again") { session.outbox?.retry(item.id) }
                            .buttonStyle(.plain).underline().frame(minHeight: 44)
                    }
                    if item.status != .sending, !item.isSent {
                        Button("Delete") { session.outbox?.delete(item.id) }
                            .buttonStyle(.plain).underline().frame(minHeight: 44)
                    }
                }
                .foregroundStyle(theme.haze)
            }
        }
        .font(.system(size: controlSize))
        .accessibilityElement(children: .contain)
    }

    private func userBubble<Content: View>(@ViewBuilder content: () -> Content) -> some View {
        content()
            .foregroundStyle(theme.frost)
            .padding(.horizontal, 16)
            .padding(.vertical, 12)
            .background(theme.deep, in: RoundedRectangle(cornerRadius: 20))
            .frame(maxWidth: .infinity, alignment: .trailing)
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
                if row.speaker == .user {
                    userBubble {
                        Text(verbatim: row.text)
                            .font(.system(size: controlSize))
                            .lineSpacing(2)
                            .textSelection(.enabled)
                    }
                } else {
                    Text(spokenText(row))
                        .font(.system(size: scriptSize, design: .serif))
                        .lineSpacing(5)
                        .foregroundStyle(theme.frost)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .textSelection(.enabled)
                }
            }
            if row.id.hasPrefix("reply:"), session.playback.missedReplies.contains(String(row.id.dropFirst(6))),
               session.playback.replyID != String(row.id.dropFirst(6)) {
                Button("Play") { Task { await session.replay(replyID: String(row.id.dropFirst(6))) } }
                    .buttonStyle(.plain)
                    .font(.system(size: controlSize))
                    .foregroundStyle(theme.haze)
                    .underline()
                    .frame(minWidth: 44, minHeight: 44)
                    .contentShape(Rectangle())
                    .disabled(!session.voiceOn || !session.connection.canSend)
                    .accessibilityLabel("Play reply")
                if let message = session.replayMessages[String(row.id.dropFirst(6))] ?? session.playback.messages[String(row.id.dropFirst(6))] {
                    Text(message).font(.system(size: controlSize)).foregroundStyle(theme.haze)
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
                .foregroundStyle(theme.haze)
                .tint(theme.haze)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
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
        return AttributedString(row.text[..<range.lowerBound]) + marked + AttributedString(row.text[range.upperBound...])
    }

}
