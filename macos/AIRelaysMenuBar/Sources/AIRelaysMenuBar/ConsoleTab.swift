import SwiftUI

/// Output of app-run commands (serve, logins, doctor, token operations),
/// plus quick access to diagnostics and files.
struct ConsoleTab: View {
    @ObservedObject var controller: RelayController

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 8) {
                Button("Doctor") {
                    controller.runDoctor()
                }
                Button("Doctor (skip response)") {
                    controller.runDoctor(skipResponse: true)
                }
                Spacer()
                Button("Open Logs Folder") {
                    controller.openLogsFolder()
                }
                Button("Open Config File") {
                    controller.openConfigFile()
                }
                Button("Clear") {
                    controller.clearConsole()
                }
            }
            consoleList
        }
        .padding(12)
    }

    private var consoleList: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 5) {
                    ForEach(controller.consoleEntries) { entry in
                        HStack(alignment: .top, spacing: 8) {
                            Text(entry.timestampLabel)
                                .foregroundStyle(.tertiary)
                            Text(entry.source)
                                .foregroundStyle(.secondary)
                                .frame(width: 90, alignment: .leading)
                            Text(entry.text)
                                .foregroundStyle(entry.isError ? .red : .primary)
                                .textSelection(.enabled)
                                .frame(maxWidth: .infinity, alignment: .leading)
                        }
                        .font(.system(.caption, design: .monospaced))
                        .id(entry.id)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(8)
            }
            .background(Color(nsColor: .textBackgroundColor), in: RoundedRectangle(cornerRadius: 8))
            .onChange(of: controller.consoleEntries.count) {
                if let lastID = controller.consoleEntries.last?.id {
                    proxy.scrollTo(lastID, anchor: .bottom)
                }
            }
        }
    }
}
