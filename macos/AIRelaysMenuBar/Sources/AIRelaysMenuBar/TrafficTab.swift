import SwiftUI

/// Request-oriented view of the relay's JSONL traffic logs: a sortable table
/// on top, the selected request's phase-by-phase detail below.
struct TrafficTab: View {
    @ObservedObject var controller: RelayController

    var body: some View {
        VSplitView {
            requestTable
                .frame(minHeight: 220)
            detailPane
                .frame(minHeight: 160)
        }
        .padding(12)
    }

    private var requestTable: some View {
        Table(controller.requestSummaries, selection: $controller.selectedRequestID) {
            TableColumn("Time") { summary in
                Text(summary.timestampLabel)
                    .font(.system(.body, design: .monospaced))
            }
            .width(min: 80, ideal: 90, max: 110)
            TableColumn("Route") { summary in
                Text(summary.routeLabel)
                    .font(.system(.body, design: .monospaced))
            }
            .width(min: 200, ideal: 320)
            TableColumn("Provider") { summary in
                Text(summary.provider)
            }
            .width(min: 70, ideal: 90, max: 120)
            TableColumn("Model") { summary in
                Text(summary.model)
            }
            .width(min: 90, ideal: 130)
            TableColumn("Status") { summary in
                statusLabel(summary.statusCode)
            }
            .width(min: 50, ideal: 60, max: 80)
            TableColumn("Last Phase") { summary in
                Text(summary.lastPhase)
                    .foregroundStyle(.secondary)
            }
            .width(min: 110, ideal: 150)
            TableColumn("Events") { summary in
                Text("\(summary.eventCount)")
                    .frame(maxWidth: .infinity, alignment: .trailing)
            }
            .width(min: 45, ideal: 55, max: 70)
        }
        .overlay {
            if controller.requestSummaries.isEmpty {
                ContentUnavailableView(
                    "No requests yet",
                    systemImage: "tray",
                    description: Text("Requests appear here as clients use the relay.")
                )
            }
        }
    }

    private func statusLabel(_ code: Int?) -> some View {
        let text = code.map(String.init) ?? "—"
        let color: Color
        switch code {
        case .some(let value) where value >= 500:
            color = .red
        case .some(let value) where value >= 400:
            color = .orange
        case .some:
            color = .green
        case .none:
            color = .secondary
        }
        return Text(text)
            .font(.system(.body, design: .monospaced))
            .foregroundStyle(color)
    }

    private var detailPane: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Request Detail")
                .font(.headline)
            ScrollView {
                Text(controller.selectedRequestDetails)
                    .font(.system(.caption, design: .monospaced))
                    .textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
        .padding(.top, 8)
    }
}
