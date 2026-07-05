import Foundation

/// Local network introspection used to show reachable relay URLs.
enum NetworkInfo {
    /// IPv4 addresses of active non-loopback interfaces (Wi-Fi, Ethernet),
    /// i.e. the addresses LAN devices can use to reach this Mac.
    static func privateIPv4Addresses() -> [String] {
        var addresses: [String] = []
        var interfaceList: UnsafeMutablePointer<ifaddrs>?
        guard getifaddrs(&interfaceList) == 0, let firstInterface = interfaceList else {
            return addresses
        }
        defer { freeifaddrs(interfaceList) }

        var pointer: UnsafeMutablePointer<ifaddrs>? = firstInterface
        while let interface = pointer {
            defer { pointer = interface.pointee.ifa_next }
            guard let addressPointer = interface.pointee.ifa_addr,
                  addressPointer.pointee.sa_family == UInt8(AF_INET) else {
                continue
            }
            let flags = Int32(interface.pointee.ifa_flags)
            let isUp = (flags & IFF_UP) == IFF_UP
            let isLoopback = (flags & IFF_LOOPBACK) == IFF_LOOPBACK
            guard isUp, !isLoopback else {
                continue
            }
            var host = [CChar](repeating: 0, count: Int(NI_MAXHOST))
            let resolved = getnameinfo(
                addressPointer,
                socklen_t(addressPointer.pointee.sa_len),
                &host,
                socklen_t(host.count),
                nil,
                0,
                NI_NUMERICHOST
            )
            if resolved == 0 {
                let bytes = host.prefix(while: { $0 != 0 }).map { UInt8(bitPattern: $0) }
                let address = String(decoding: bytes, as: UTF8.self)
                if !address.isEmpty && !addresses.contains(address) {
                    addresses.append(address)
                }
            }
        }
        return addresses
    }
}
