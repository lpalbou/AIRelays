import SwiftUI

/// Shared building blocks for the dashboard and settings views.

func badgeKind(for lifecycle: RelayLifecycle) -> StatusBadge.Kind {
    switch lifecycle {
    case .running:
        return .active
    case .starting, .stopping:
        return .warning
    case .failed:
        return .danger
    case .stopped:
        return .neutral
    }
}

struct SectionCard<Content: View>: View {
    let title: String
    var caption: String?
    @ViewBuilder var content: Content

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            VStack(alignment: .leading, spacing: 2) {
                Text(title)
                    .font(.headline)
                if let caption {
                    Text(caption)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
            content
        }
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
    }
}

struct StatusDot: View {
    let kind: StatusBadge.Kind

    var body: some View {
        Circle()
            .fill(kind.foreground)
            .frame(width: 11, height: 11)
    }
}

struct StatusBadge: View {
    enum Kind {
        case active
        case warning
        case danger
        case neutral

        var foreground: Color {
            switch self {
            case .active:
                return .green
            case .warning:
                return .orange
            case .danger:
                return .red
            case .neutral:
                return .secondary
            }
        }

        var background: Color {
            foreground.opacity(0.12)
        }
    }

    let text: String
    let kind: Kind

    var body: some View {
        Text(text)
            .font(.caption.weight(.semibold))
            .foregroundStyle(kind.foreground)
            .padding(.horizontal, 10)
            .padding(.vertical, 5)
            .background(kind.background, in: Capsule())
    }
}
