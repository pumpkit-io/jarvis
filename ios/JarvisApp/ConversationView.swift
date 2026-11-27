import JarvisClient
import SwiftUI

struct ConversationView: View {
    @Bindable var session: AppSession
    var showsInputBar = true
    var typingChanged: (Bool) -> Void = { _ in }
    @ScaledMetric(relativeTo: .body) var scriptSize = 18
    @ScaledMetric(relativeTo: .subheadline) var controlSize = 15
    @ScaledMetric(relativeTo: .caption) var stampSize = 12
    @State private var followsLatest = true
    @State private var isAtBottom = true
    @State private var detailsChanging = false
    @State private var detailsRevision = 0
    @State private var position = ScrollPosition(idType: String.self)
    @State private var scrollPhase: ScrollPhase = .idle
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var olderHistoryVisible = false
    @State var expandedDetails: Set<String> = []
    private var theme: JarvisTheme { JarvisTheme() }

    /// Messages, stamps, Play and Details use the system monospaced face.
    private func mono(_ size: CGFloat) -> Font { .system(size: size, design: .monospaced) }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            ScrollView {
                VStack(alignment: .leading, spacing: 32) {
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
                        .modifier(ScriptColumn())
                        // The stack builds every row up front, so paging waits until the reader reaches the top.
                        .onScrollVisibilityChange(threshold: 0.1) { olderHistoryVisible = $0 }
                        .task(id: OlderHistoryTrigger(visible: olderHistoryVisible, canSend: session.connection.canSend)) {
                            guard olderHistoryVisible else { return }
                            await session.loadOlderHistory()
                        }
                    }
                    if let message = session.historyMessage {
                        Text(message).font(.system(size: controlSize)).foregroundStyle(theme.haze)
                            .modifier(ScriptColumn())
                    }
                    if session.conversation.rows.isEmpty && (session.outbox?.items.isEmpty ?? true) {
                        Text("Ask Jarvis anything.")
                            .font(.system(size: controlSize))
                            .foregroundStyle(theme.haze)
                            .lineSpacing(5)
                            .padding(.top, 24)
                            .modifier(ScriptColumn())
                    }
                    ForEach(session.conversation.rows) { row in
                        scriptRow(row).modifier(ScriptColumn()).id(row.id)
                    }
                    ForEach(session.outbox?.items ?? []) { item in
                        queued(item).modifier(ScriptColumn()).id("outbox:\(item.id)")
                    }
                }
                .scrollTargetLayout()
                .foregroundStyle(theme.frost)
                .padding(.vertical, 20)
                .containerRelativeFrame(.horizontal)
            }
            .scrollPosition($position, anchor: .top)
            .defaultScrollAnchor(.bottom, for: .initialOffset)
            .defaultScrollAnchor(followsLatest ? .bottom : nil, for: .sizeChanges)
            .defaultScrollAnchor(.top, for: .alignment)
            .onScrollGeometryChange(for: CGFloat.self) { $0.containerSize.height } action: { old, new in
                // A reader's drag wins over pinning the script to its end.
                guard old != new, followsLatest, scrollPhase == .idle else { return }
                position.scrollTo(edge: .bottom)
            }
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
        // The conversation ends where the input bar starts and follows the keyboard, so the button rides above both.
        .overlay(alignment: .bottomTrailing) {
            if showsScrollToLatest {
                ScrollToLatestButton(action: scrollToLatest)
                    .padding(.trailing, 18)
                    .padding(.bottom, 8)
                    .transition(reduceMotion ? .opacity : .scale(scale: 0.5).combined(with: .opacity))
            }
        }
        .animation(.spring(response: 0.3, dampingFraction: 0.85), value: showsScrollToLatest)
        .safeAreaInset(edge: .bottom, spacing: 0) {
            if showsInputBar { MessageInputBar(session: session, typingChanged: typingChanged) }
        }
    }

    /// A held Talk's lock capsule and the composer's notices fill the corner above the pill, so the button steps aside for them.
    private var showsScrollToLatest: Bool {
        !isAtBottom && session.talk.state != .recording && session.talk.state != .paused && !session.hasComposerNotice
    }

    private func scrollToLatest() {
        followsLatest = true
        withAnimation(.easeInOut(duration: 0.3)) { position.scrollTo(edge: .bottom) }
    }

    /// A message still on this phone, shown where the User's line will appear once Jarvis hears it.
    private func queued(_ item: OutboxItem) -> some View {
        messageFrame(speaker: .user, date: item.recordedAt) {
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
        .font(mono(controlSize))
        .accessibilityElement(children: .contain)
    }

    /// Places a message's date and time above it, and puts the User's line in its bubble.
    @ViewBuilder private func messageFrame<Content: View>(speaker: ConversationRow.Speaker, date: Date?,
                                                         @ViewBuilder content: () -> Content) -> some View {
        let user = speaker == .user
        VStack(alignment: user ? .trailing : .leading, spacing: 6) {
            stamp(date)
            if user { userBubble(content: content) } else { content() }
        }
        .frame(maxWidth: .infinity, alignment: user ? .trailing : .leading)
    }

    @ViewBuilder private func stamp(_ date: Date?) -> some View {
        if let date {
            Text(verbatim: MessageStamp(date).text)
                .font(mono(stampSize))
                .foregroundStyle(theme.haze)
        }
    }

    private func userBubble<Content: View>(@ViewBuilder content: () -> Content) -> some View {
        content()
            .foregroundStyle(theme.frost)
            .padding(.horizontal, 16)
            .padding(.vertical, 12)
            .background(theme.deep, in: RoundedRectangle(cornerRadius: 20))
            .frame(maxWidth: .infinity, alignment: .trailing)
    }

    @ViewBuilder private func scriptRow(_ row: ConversationRow) -> some View {
        if row.speaker == .user {
            VStack(alignment: .leading, spacing: 12) {
                if !row.text.isEmpty {
                    messageFrame(speaker: .user, date: row.date) {
                        Text(verbatim: row.text)
                            .font(mono(controlSize))
                            .lineSpacing(2)
                            .textSelection(.enabled)
                    }
                }
                rowControls(row)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        } else if !row.text.isEmpty || showsPlay(row) || !row.details.isEmpty {
            messageFrame(speaker: row.speaker, date: row.date) {
                VStack(alignment: .leading, spacing: 12) {
                    if !row.text.isEmpty {
                        Text(spokenText(row))
                            .font(mono(scriptSize))
                            .lineSpacing(5)
                            .foregroundStyle(theme.frost)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .textSelection(.enabled)
                    }
                    rowControls(row)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
    }

    private func showsPlay(_ row: ConversationRow) -> Bool {
        row.id.hasPrefix("reply:") && session.playback.missedReplies.contains(String(row.id.dropFirst(6)))
            && session.playback.replyID != String(row.id.dropFirst(6))
    }

    @ViewBuilder private func rowControls(_ row: ConversationRow) -> some View {
        if showsPlay(row) {
            Button("Play") { Task { await session.replay(replyID: String(row.id.dropFirst(6))) } }
                .buttonStyle(.plain)
                .font(mono(controlSize))
                .foregroundStyle(theme.haze)
                .underline()
                .frame(minWidth: 44, minHeight: 44)
                .contentShape(Rectangle())
                .disabled(!session.voiceOn || !session.connection.canSend)
                .accessibilityLabel("Play reply")
            if let message = session.replayMessages[String(row.id.dropFirst(6))] ?? session.playback.messages[String(row.id.dropFirst(6))] {
                Text(message).font(mono(controlSize)).foregroundStyle(theme.haze)
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
            .font(mono(controlSize))
            .foregroundStyle(theme.haze)
            .tint(theme.haze)
            .frame(maxWidth: .infinity, alignment: .leading)
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
        return AttributedString(row.text[..<range.lowerBound]) + marked + AttributedString(row.text[range.upperBound...])
    }

}

/// Telegram's scroll-to-bottom button: a 40-point round, blurred dark disc with a down chevron, in a 44-point target.
private struct ScrollToLatestButton: View {
    let action: () -> Void
    private let theme = JarvisTheme()

    var body: some View {
        Button(action: action) {
            Image(systemName: "chevron.down")
                .font(.system(size: 17, weight: .semibold))
                .foregroundStyle(theme.frost)
                .frame(width: 40, height: 40)
                .background(.ultraThinMaterial, in: Circle())
                .background(theme.deep.opacity(0.8), in: Circle())
                .overlay { Circle().strokeBorder(theme.rim, lineWidth: 1) }
                .frame(width: 44, height: 44)
                .contentShape(Circle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel("Scroll to latest message")
    }
}

private struct OlderHistoryTrigger: Equatable {
    let visible: Bool
    let canSend: Bool
}

/// Every scroll target spans the viewport from x = 0, with the column's margins inside it.
/// iOS 26 scrolls sideways to a restored row's leading edge, so a row inset by the margin would shift the script.
private struct ScriptColumn: ViewModifier {
    func body(content: Content) -> some View {
        content
            .frame(maxWidth: 640, alignment: .leading)
            .padding(.horizontal, 20)
            .frame(maxWidth: .infinity)
    }
}
