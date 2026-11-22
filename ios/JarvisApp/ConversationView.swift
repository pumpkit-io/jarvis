import JarvisClient
import SwiftUI

struct ConversationView: View {
    @Bindable var session: AppSession
    var showsInputBar = true
    var typingChanged: (Bool) -> Void = { _ in }
    @ScaledMetric(relativeTo: .body) var scriptSize = 18
    @ScaledMetric(relativeTo: .subheadline) var controlSize = 15
    @ScaledMetric(relativeTo: .caption) var stampSize = 12
    @ScaledMetric(relativeTo: .caption) var gutterWidth = 56
    @Environment(ScriptStyleSetting.self) private var scriptStyle
    @State private var followsLatest = true
    @State private var isAtBottom = true
    @State private var detailsChanging = false
    @State private var detailsRevision = 0
    @State private var position = ScrollPosition(idType: String.self)
    @State private var scrollPhase: ScrollPhase = .idle
    @State private var olderHistoryVisible = false
    @State var expandedDetails: Set<String> = []
    private var theme: JarvisTheme { JarvisTheme() }
    private var look: ScriptStyle.Look { scriptStyle.style.look }
    private var face: ScriptFont { ScriptFont.with(id: scriptStyle.style.font) }

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
        .safeAreaInset(edge: .bottom, spacing: 0) {
            if showsInputBar { MessageInputBar(session: session, typingChanged: typingChanged) }
        }
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
        .font(face.font(size: controlSize))
        .accessibilityElement(children: .contain)
    }

    /// Places a message's date and time, and the User's styling, the way the chosen look draws them.
    @ViewBuilder private func messageFrame<Content: View>(speaker: ConversationRow.Speaker, date: Date?,
                                                         @ViewBuilder content: () -> Content) -> some View {
        let user = speaker == .user
        switch look {
        case .current:
            VStack(alignment: user ? .trailing : .leading, spacing: 6) {
                stamp(date)
                if user { userBubble(content: content) } else { content() }
            }
            .frame(maxWidth: .infinity, alignment: user ? .trailing : .leading)
        case .terminal:
            VStack(alignment: .leading, spacing: 4) {
                HStack(alignment: .firstTextBaseline, spacing: 10) {
                    Text(speakerName(speaker)).foregroundStyle(user ? theme.cyan : theme.frost)
                    stamp(date)
                }
                .font(face.font(size: stampSize))
                content()
                    .foregroundStyle(user ? theme.ice : theme.frost)
                    .padding(.leading, 12)
                    .overlay(alignment: .leading) {
                        if user { Rectangle().fill(theme.cyan).frame(width: 2) }
                    }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        case .gutter:
            HStack(alignment: .top, spacing: 12) {
                VStack(alignment: .trailing, spacing: 2) {
                    if let date {
                        let stamp = MessageStamp(date)
                        Text(verbatim: stamp.day).lineLimit(1).minimumScaleFactor(0.7)
                        Text(verbatim: stamp.time).lineLimit(1).foregroundStyle(user ? theme.cyan : theme.haze)
                    }
                }
                .font(face.font(size: stampSize)).monospacedDigit()
                .foregroundStyle(theme.haze)
                .frame(width: gutterWidth, alignment: .trailing)
                .padding(.top, user ? 10 : 2)
                if user {
                    content()
                        .foregroundStyle(theme.frost)
                        .padding(.horizontal, 12).padding(.vertical, 10)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .background(theme.deep)
                        .overlay(alignment: .leading) { Rectangle().fill(theme.cyan).frame(width: 2) }
                } else {
                    content().frame(maxWidth: .infinity, alignment: .leading)
                }
            }
            // Without a fixed vertical size the HStack truncates long Details text instead of wrapping it.
            .fixedSize(horizontal: false, vertical: true)
        }
    }

    @ViewBuilder private func stamp(_ date: Date?) -> some View {
        if let date {
            Text(verbatim: MessageStamp(date).text)
                .font(face.font(size: stampSize)).monospacedDigit()
                .foregroundStyle(theme.haze)
        }
    }

    private func speakerName(_ speaker: ConversationRow.Speaker) -> String {
        switch speaker {
        case .user: "you"
        case .jarvis: "jarvis"
        case .notice: "notice"
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
                            .font(face.font(size: controlSize))
                            .lineSpacing(2)
                            .textSelection(.enabled)
                    }
                }
                rowControls(row)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        } else {
            messageFrame(speaker: row.speaker, date: row.date) {
                VStack(alignment: .leading, spacing: 12) {
                    if !row.text.isEmpty {
                        Text(spokenText(row))
                            .font(face.font(size: scriptSize, reply: true))
                            .lineSpacing(5)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .textSelection(.enabled)
                    }
                    rowControls(row)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
    }

    /// Deliberate Play and Details, which every look keeps with the same behaviour.
    @ViewBuilder private func rowControls(_ row: ConversationRow) -> some View {
        if row.id.hasPrefix("reply:"), session.playback.missedReplies.contains(String(row.id.dropFirst(6))),
           session.playback.replyID != String(row.id.dropFirst(6)) {
            Button("Play") { Task { await session.replay(replyID: String(row.id.dropFirst(6))) } }
                .buttonStyle(.plain)
                .font(face.font(size: controlSize))
                .foregroundStyle(theme.haze)
                .underline()
                .frame(minWidth: 44, minHeight: 44)
                .contentShape(Rectangle())
                .disabled(!session.voiceOn || !session.connection.canSend)
                .accessibilityLabel("Play reply")
            if let message = session.replayMessages[String(row.id.dropFirst(6))] ?? session.playback.messages[String(row.id.dropFirst(6))] {
                Text(message).font(face.font(size: controlSize)).foregroundStyle(theme.haze)
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
            .font(face.font(size: controlSize))
            .foregroundStyle(theme.haze)
            .tint(theme.haze)
            .frame(maxWidth: .infinity, alignment: .leading)
            .modifier(DetailsLook(look: look, count: row.details.count))
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

/// Temporary: the experimental looks restyle Details; the Current look keeps the native disclosure.
/// Every style toggles the same binding, so opening, closing and following the latest text behave alike.
private struct DetailsLook: ViewModifier {
    let look: ScriptStyle.Look
    let count: Int

    func body(content: Content) -> some View {
        switch look {
        case .current: content
        case .terminal: content.disclosureGroupStyle(TreeDisclosureStyle(count: count))
        case .gutter: content.disclosureGroupStyle(PanelDisclosureStyle(count: count))
        }
    }
}

/// A toggle line with the entry count, and the entries hanging off a rule like a tree listing.
private struct TreeDisclosureStyle: DisclosureGroupStyle {
    let count: Int
    private let theme = JarvisTheme()

    func makeBody(configuration: Configuration) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            Button { configuration.isExpanded.toggle() } label: {
                HStack(spacing: 8) {
                    Text(verbatim: configuration.isExpanded ? "▾" : "▸").foregroundStyle(theme.cyan)
                    configuration.label
                    Text(verbatim: "(\(count))")
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityValue(configuration.isExpanded ? "Expanded" : "Collapsed")
            if configuration.isExpanded {
                VStack(alignment: .leading, spacing: 0) { configuration.content }
                    .padding(.leading, 16)
                    .overlay(alignment: .leading) { Rectangle().fill(theme.rim).frame(width: 1).padding(.leading, 4) }
            }
        }
    }
}

/// A framed panel whose header row opens it, with the entries below a rule.
private struct PanelDisclosureStyle: DisclosureGroupStyle {
    let count: Int
    private let theme = JarvisTheme()

    func makeBody(configuration: Configuration) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            Button { configuration.isExpanded.toggle() } label: {
                HStack(spacing: 8) {
                    configuration.label
                    Spacer(minLength: 8)
                    Text(count, format: .number).monospacedDigit()
                    Image(systemName: "chevron.down")
                        .rotationEffect(.degrees(configuration.isExpanded ? 180 : 0))
                        .accessibilityHidden(true)
                }
                .padding(.horizontal, 12)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityValue(configuration.isExpanded ? "Expanded" : "Collapsed")
            if configuration.isExpanded {
                Rectangle().fill(theme.rim).frame(height: 1)
                VStack(alignment: .leading, spacing: 0) { configuration.content }
                    .padding(.horizontal, 12)
                    .padding(.vertical, 4)
            }
        }
        .background(theme.deep.opacity(0.6), in: RoundedRectangle(cornerRadius: 10))
        .overlay(RoundedRectangle(cornerRadius: 10).strokeBorder(theme.rim))
    }
}
