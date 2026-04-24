# Native iOS App for Cloude Code - Complete Implementation Plan

**Project Location:** `./ios/` (keep iOS app separate from main project)

---

## Table of Contents
1. [Overview](#overview)
2. [Research: Terminal Emulation Libraries](#research-terminal-emulation-libraries)
3. [Technology Stack](#technology-stack)
4. [App Architecture](#app-architecture)
5. [Project Structure](#project-structure)
6. [Key Implementation Details](#key-implementation-details)
7. [Implementation Phases](#implementation-phases)
8. [Code Samples](#code-samples)
9. [Benefits Over Web UI](#benefits-over-web-ui)
10. [Challenges & Solutions](#challenges--solutions)
11. [Timeline](#timeline)

---

## Overview

Build a native Swift iOS app that replaces the web browser client for Cloude Code. The app will:
- Use **SwiftTerm** for terminal emulation
- Connect to existing FastAPI backend via **WebSocket + REST API**
- Provide native iOS experience (keyboard, gestures, App Store distribution)
- Be built in `./ios/` folder to keep project organized

**Target:** iOS 15+ (iPhone & iPad)

---

## Research: Terminal Emulation Libraries

### SwiftTerm ⭐ **RECOMMENDED**

**Repository:** https://github.com/migueldeicaza/SwiftTerm
**License:** MIT (commercial use allowed)
**Stars:** 1,110
**Maintenance:** Active (last commit: 5 days ago)

#### Why SwiftTerm Wins:
1. **Production-proven** - Used in commercial apps:
   - Secure Shellfish (https://secureshellfish.app)
   - La Terminal (https://la-terminal.net)
   - CodeEdit (https://www.codeedit.app)

2. **Complete VT100/Xterm emulation** - better than xterm.js in many ways

3. **Full ANSI support** - ANSI, 256-color, TrueColor (24-bit)

4. **Both UIKit AND SwiftUI** - UI-agnostic engine with platform-specific frontends

5. **Excellent docs** - https://migueldeicaza.github.io/SwiftTermDocs/documentation/swiftterm/

6. **Working examples** - SwiftTermApp repo shows full SwiftUI implementation

7. **Swift Package Manager** - easy integration

8. **Advanced features:**
   - Unicode rendering (emoji, combining characters, grapheme clusters)
   - Mouse event support
   - iTerm2 image protocol
   - Resize events via delegate
   - External keyboard support
   - Binary I/O handling

#### PROS:
- Battle-tested in commercial apps
- Full ANSI/VT100/xterm support
- Both UIKit and SwiftUI compatible
- Excellent documentation
- Active maintenance
- MIT license

#### CONS:
- iOS keyboard quirks (safe area issues with keyboard accessory view)
- No built-in SSH (you wire your own network layer)
- Backspace auto-repeat bug (known issue on iOS)

#### Key API:
```swift
// Send input to terminal (user typing)
terminalView.send(text: "ls\n")
terminalView.send(data: Data([0x03])) // Ctrl-C

// Receive output from server
terminalView.feed(byteArray: [UInt8](serverData))

// Get terminal size
let cols = terminalView.getTerminal().cols
let rows = terminalView.getTerminal().rows

// Resize programmatically
terminalView.getTerminal().resize(cols: 80, rows: 24)
```

#### SwiftTerm Delegate Protocol:
```swift
protocol TerminalViewDelegate {
    // Required: Send data out (to WebSocket/SSH)
    func send(source: TerminalView, data: ArraySlice<UInt8>)

    // Optional: Handle size changes
    func sizeChanged(source: TerminalView, newCols: Int, newRows: Int)

    // Optional: Cursor visibility changes
    func setTerminalTitle(source: TerminalView, title: String)
    func hostCurrentDirectoryUpdate(source: TerminalView, directory: String?)
}
```

### Alternatives Considered (NOT RECOMMENDED)

#### NewTerm 3
- **Type:** Jailbreak app (not a library)
- **Why not:** Requires jailbreak, can't ship on App Store
- **Features:** 120fps ProMotion, split-screen (could extract terminal component but overkill)

#### Terminal (dnpp73)
- **Type:** WebView wrapper (hterm.js in WKWebView)
- **Why not:** Web-based, performance hit, not native rendering

#### OpenTerm
- **Type:** Sandboxed local shell app
- **Why not:** Local-only, can't connect to remote servers

### Comparison Table

| Feature | SwiftTerm | NewTerm 3 | dnpp73/Terminal | OpenTerm |
|---------|-----------|-----------|-----------------|----------|
| **Type** | Library | App (jailbreak) | WebView wrapper | Local shell app |
| **UIKit** | ✅ | ✅ | ✅ | ✅ |
| **SwiftUI** | ✅ (examples) | ✅ | ❌ | ❌ |
| **App Store** | ✅ | ❌ | ✅ | ✅ |
| **VT100** | ✅ | ✅ | ✅ (hterm.js) | ✅ |
| **TrueColor** | ✅ | ✅ | ✅ | Unknown |
| **120fps** | ❌ | ✅ | ❌ | ❌ |
| **Native rendering** | ✅ | ✅ | ❌ (web) | ✅ |
| **Remote SSH** | ✅ (DIY) | ✅ | ✅ | ❌ |
| **Active dev** | ✅ | ✅ (beta) | ❌ (old) | ❌ (old) |
| **Commercial apps** | 3+ | 0 | 0 | 0 |

---

## Technology Stack

- **SwiftUI** - Main UI framework (iOS 15+)
- **SwiftTerm** - Terminal emulator (UIKit view wrapped in SwiftUI)
- **URLSession WebSocket** - Binary WebSocket for terminal I/O (native, no dependencies)
- **Keychain** - Secure JWT token storage
- **Combine** - Reactive state management
- **Swift Package Manager** - Dependency management

**No third-party dependencies besides SwiftTerm** - keep it lean.

---

## App Architecture

### Navigation Flow

```
App Launch
  ↓
Check Keychain for JWT
  ↓
┌─────────────┬─────────────┐
│ No Token    │ Valid Token │
↓             ↓             │
ServerSetup   Check Session │
↓             ↓             │
TOTPLogin     ┌─────────┬───┘
↓             │ Exists  │ None
└─────────────→ Terminal→ Launchpad
                         ↓
                    Select/Create Project
                         ↓
                      Terminal
```

### Core Screens

#### 1. **Authentication Flow**
- **ServerSetupView** - Enter server URL (e.g., `http://192.168.1.100:8000`)
- **TOTPLoginView** - 6-digit TOTP code input
- **Optional:** QRScannerView - Scan TOTP QR code from setup_auth.py
- JWT token storage in Keychain
- Auto-login if valid token exists

#### 2. **Launchpad Screen**
- Project list (same as web UI)
- Pull to refresh
- Create new project (name + description modal)
- Delete projects (swipe to delete with confirmation)
- Connect to existing session detection
- Most recently used auto-sort
- Empty state: "No projects yet"

#### 3. **Terminal Screen**
- SwiftTerm view (full screen)
- Tunnel bar at top (show active tunnels with tap-to-copy URLs)
- Keyboard accessory bar (Esc, Tab, Ctrl+C, Ctrl+D, arrows)
- Session info in nav bar (session name, status dot)
- Hardware keyboard support (iPad/external keyboard)
- Reconnecting indicator during WebSocket reconnect

#### 4. **Settings Screen**
- Server URL configuration (change server)
- Logout button (clear Keychain, disconnect WebSocket)
- App version info
- Re-scan TOTP QR code option
- About section

---

## Project Structure

**Location:** `./ios/CloudeCode/`

```
ios/
└── CloudeCode/                          # Xcode project
    ├── CloudeCode.xcodeproj
    ├── CloudeCode/
    │   ├── CloudeCodeApp.swift         # Main entry point (@main)
    │   ├── AppState.swift              # Global observable state
    │   ├── Info.plist
    │   ├── Assets.xcassets/            # Icons, colors
    │   │
    │   ├── Views/
    │   │   ├── Auth/
    │   │   │   ├── ServerSetupView.swift       # Enter server URL
    │   │   │   ├── TOTPLoginView.swift         # 6-digit code entry
    │   │   │   └── QRScannerView.swift         # Optional: scan TOTP QR
    │   │   │
    │   │   ├── Launchpad/
    │   │   │   ├── LaunchpadView.swift         # Project list
    │   │   │   ├── ProjectRow.swift            # Project list item
    │   │   │   └── NewProjectSheet.swift       # Create project modal
    │   │   │
    │   │   ├── Terminal/
    │   │   │   ├── TerminalContainerView.swift # SwiftUI wrapper
    │   │   │   ├── TerminalViewController.swift # UIKit controller
    │   │   │   ├── TunnelBarView.swift         # Show active tunnels
    │   │   │   └── KeyboardAccessoryView.swift # Esc/Tab/Ctrl buttons
    │   │   │
    │   │   └── Settings/
    │   │       └── SettingsView.swift
    │   │
    │   ├── Services/
    │   │   ├── APIService.swift                # REST API calls
    │   │   ├── WebSocketService.swift          # WebSocket manager
    │   │   ├── AuthService.swift               # TOTP/JWT handling
    │   │   └── KeychainService.swift           # Secure storage
    │   │
    │   ├── Models/
    │   │   ├── Session.swift                   # Session data model
    │   │   ├── Project.swift                   # Project data model
    │   │   ├── Tunnel.swift                    # Tunnel data model
    │   │   └── ServerConfig.swift              # Server settings
    │   │
    │   └── Extensions/
    │       ├── Data+Hex.swift                  # Binary helpers
    │       └── View+Extensions.swift           # SwiftUI helpers
    │
    └── CloudeCodeTests/                # Unit tests
        └── ...
```

---

## Key Implementation Details

### 1. WebSocket Binary I/O

**URLSession WebSocket** natively supports binary messages (iOS 13+):

```swift
// Send binary data (terminal input)
let data = Data([0x1b, 0x5b, 0x41]) // Up arrow escape sequence
webSocket?.send(.data(data)) { error in
    if let error = error {
        print("Send error: \(error)")
    }
}

// Receive binary data (terminal output)
webSocket?.receive { result in
    switch result {
    case .success(let message):
        switch message {
        case .data(let data):
            // Feed to SwiftTerm
            terminalView.feed(byteArray: [UInt8](data))
        case .string(let text):
            // Handle JSON messages (tunnels, logs)
            handleJSONMessage(text)
        @unknown default:
            break
        }
    case .failure(let error):
        print("Receive error: \(error)")
    }
}
```

**Data Flow:**
```
User types in terminal
  → SwiftTerm calls delegate: send(source:data:)
  → data is ArraySlice<UInt8>
  → Convert to Data
  → WebSocket.send(.data(Data(data)))
  → Server receives binary WebSocket frame

Server sends PTY output
  → Binary WebSocket frame
  → URLSessionWebSocketTask receives .data(Data)
  → Convert to [UInt8]
  → terminalView.feed(byteArray: [UInt8](data))
  → SwiftTerm renders
```

### 2. Authentication Flow

#### Step 1: Server Setup
```swift
// User enters: http://192.168.1.100:8000
// Save to UserDefaults
UserDefaults.standard.set("http://192.168.1.100:8000", forKey: "serverURL")
```

#### Step 2: TOTP Login
```swift
// POST /api/v1/auth/totp/verify
let body = ["token": "123456"]
let response = await APIService.verifyTOTP(code: "123456")
// Response: {token: "eyJhbGc...", expires_in: 1800}
```

#### Step 3: JWT Storage
```swift
// Store in Keychain (secure, persists across app launches)
KeychainService.save(token: response.token)
```

#### Step 4: API Authentication
```swift
// All REST API calls
var request = URLRequest(url: url)
request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
```

#### Step 5: WebSocket Authentication
```swift
// Token in query parameter (WebSocket doesn't support custom headers)
let wsURL = "ws://\(serverURL)/ws/terminal?token=\(token)"
webSocket = URLSession.shared.webSocketTask(with: URL(string: wsURL)!)
```

#### Step 6: Token Expiry Handling
```swift
// Handle 401 Unauthorized responses
if response.statusCode == 401 {
    KeychainService.deleteToken()
    // Navigate to login screen
    appState.navigationPath = .login
}
```

### 3. SwiftTerm Integration

#### UIKit TerminalViewController
```swift
import SwiftTerm
import UIKit

class TerminalViewController: UIViewController, TerminalViewDelegate {
    var terminalView: TerminalView!
    var onSendData: ((Data) -> Void)?
    var onResize: ((Int, Int) -> Void)?

    override func viewDidLoad() {
        super.viewDidLoad()

        // Create SwiftTerm view
        terminalView = TerminalView(frame: view.bounds)
        terminalView.terminalDelegate = self
        terminalView.autoresizingMask = [.flexibleWidth, .flexibleHeight]
        view.addSubview(terminalView)

        // Configure options
        terminalView.getTerminal().silentLog = false
    }

    // DELEGATE: Send terminal input to server
    func send(source: TerminalView, data: ArraySlice<UInt8>) {
        onSendData?(Data(data))
    }

    // DELEGATE: Notify server of terminal resize
    func sizeChanged(source: TerminalView, newCols: Int, newRows: Int) {
        onResize?(newCols, newRows)
    }

    // PUBLIC: Feed data from server to terminal
    func feedTerminal(data: Data) {
        terminalView.feed(byteArray: [UInt8](data))
    }
}
```

#### SwiftUI Wrapper
```swift
import SwiftUI

struct TerminalContainerView: UIViewControllerRepresentable {
    @ObservedObject var webSocketService: WebSocketService
    let session: Session

    func makeUIViewController(context: Context) -> TerminalViewController {
        let controller = TerminalViewController()

        // Terminal input → WebSocket
        controller.onSendData = { data in
            webSocketService.send(data: data)
        }

        // Terminal resize → WebSocket
        controller.onResize = { cols, rows in
            webSocketService.sendResize(cols: cols, rows: rows)
        }

        // WebSocket output → Terminal
        webSocketService.onBinaryMessage = { data in
            controller.feedTerminal(data: data)
        }

        return controller
    }

    func updateUIViewController(_ uiViewController: TerminalViewController, context: Context) {
        // No updates needed
    }
}
```

### 4. Session Lifecycle

```swift
// App Launch
@main
struct CloudeCodeApp: App {
    @StateObject var appState = AppState()

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environmentObject(appState)
                .onAppear {
                    appState.checkAuthentication()
                }
        }
    }
}

// AppState
class AppState: ObservableObject {
    @Published var isAuthenticated = false
    @Published var currentSession: Session?

    func checkAuthentication() {
        // 1. Check Keychain for JWT
        guard let token = KeychainService.getToken() else {
            isAuthenticated = false
            return
        }

        // 2. Validate token
        if AuthService.isTokenExpired(token) {
            KeychainService.deleteToken()
            isAuthenticated = false
            return
        }

        // 3. Check if session exists
        Task {
            do {
                let session = try await APIService.getSession()
                currentSession = session
                isAuthenticated = true
            } catch {
                // No session exists, show launchpad
                currentSession = nil
                isAuthenticated = true
            }
        }
    }
}
```

### 5. Background Handling

iOS suspends apps when backgrounded - WebSocket will disconnect.

```swift
// In AppDelegate or SceneDelegate
func sceneDidBecomeActive(_ scene: UIScene) {
    // App resumed from background
    webSocketService.reconnect()
}

// WebSocketService
func reconnect() {
    guard let serverURL = UserDefaults.standard.string(forKey: "serverURL"),
          let token = KeychainService.getToken() else {
        return
    }

    // Reconnect with exponential backoff
    var attempt = 0
    func attemptReconnect() {
        attempt += 1
        let delay = min(pow(2.0, Double(attempt - 1)), 16.0) // 1s, 2s, 4s, 8s, 16s

        DispatchQueue.main.asyncAfter(deadline: .now() + delay) {
            connect(serverURL: serverURL, token: token)
        }
    }

    attemptReconnect()
}
```

**Server session persists** - PTY keeps running, so reconnecting picks up where you left off.

### 6. Keyboard Handling

#### External/Hardware Keyboard
SwiftTerm handles this automatically - all keys work.

#### On-Screen Keyboard
iOS keyboards lack terminal control keys. Add **keyboard accessory bar**:

```swift
class KeyboardAccessoryView: UIView {
    var onKeyPress: ((String) -> Void)?

    init() {
        super.init(frame: CGRect(x: 0, y: 0, width: UIScreen.main.bounds.width, height: 44))
        backgroundColor = .systemGray6

        let keys = [
            ("Esc", "\u{1b}"),
            ("Tab", "\t"),
            ("Ctrl+C", "\u{03}"),
            ("Ctrl+D", "\u{04}"),
            ("↑", "\u{1b}[A"),
            ("↓", "\u{1b}[B"),
            ("←", "\u{1b}[D"),
            ("→", "\u{1b}[C")
        ]

        let stackView = UIStackView()
        stackView.axis = .horizontal
        stackView.distribution = .fillEqually
        stackView.spacing = 8

        for (label, code) in keys {
            let button = UIButton(type: .system)
            button.setTitle(label, for: .normal)
            button.addAction(UIAction { [weak self] _ in
                self?.onKeyPress?(code)
            }, for: .touchUpInside)
            stackView.addArrangedSubview(button)
        }

        addSubview(stackView)
        // Layout constraints...
    }
}

// In TerminalViewController
override var inputAccessoryView: UIView? {
    let accessory = KeyboardAccessoryView()
    accessory.onKeyPress = { [weak self] code in
        self?.terminalView.send(text: code)
    }
    return accessory
}
```

#### Hardware Keyboard Shortcuts (iPadOS)
```swift
// Cmd+K = clear screen
// Cmd+D = disconnect
// Cmd+W = close terminal
override var keyCommands: [UIKeyCommand]? {
    return [
        UIKeyCommand(input: "k", modifierFlags: .command, action: #selector(clearScreen)),
        UIKeyCommand(input: "d", modifierFlags: .command, action: #selector(disconnect)),
    ]
}
```

### 7. iPad Support

- **Landscape mode** - full support, terminal takes full screen
- **External keyboard** - full support via SwiftTerm
- **Split-screen multitasking** - terminal in one pane, Safari in other (view dev server)
- **Pointer support** - iPadOS automatically adds cursor highlighting

```swift
// In Info.plist
<key>UIRequiresFullScreen</key>
<false/>  <!-- Allow split-screen -->

<key>UISupportedInterfaceOrientations~ipad</key>
<array>
    <string>UIInterfaceOrientationPortrait</string>
    <string>UIInterfaceOrientationLandscapeLeft</string>
    <string>UIInterfaceOrientationLandscapeRight</string>
    <string>UIInterfaceOrientationPortraitUpsideDown</string>
</array>
```

---

## Implementation Phases

### **Phase 1: Project Setup** (Day 1)

**Goal:** Create Xcode project and setup dependencies.

**Tasks:**
1. Create new Xcode project:
   ```bash
   cd ./ios
   # Open Xcode → New Project
   # Template: iOS App
   # Interface: SwiftUI
   # Language: Swift
   # Minimum Deployment: iOS 15.0
   ```

2. Add SwiftTerm via Swift Package Manager:
   - Xcode → File → Add Package Dependencies
   - URL: `https://github.com/migueldeicaza/SwiftTerm.git`
   - Version: "Latest" or "1.0.0"

3. Setup project structure:
   ```bash
   mkdir -p Views/{Auth,Launchpad,Terminal,Settings}
   mkdir -p Services
   mkdir -p Models
   mkdir -p Extensions
   ```

4. Create basic navigation:
   ```swift
   // ContentView.swift
   struct ContentView: View {
       @EnvironmentObject var appState: AppState

       var body: some View {
           if !appState.isAuthenticated {
               ServerSetupView()
           } else if let session = appState.currentSession {
               TerminalView(session: session)
           } else {
               LaunchpadView()
           }
       }
   }
   ```

**Deliverable:** Empty app launches, SwiftTerm imported, folder structure ready.

---

### **Phase 2: Authentication** (Day 2)

**Goal:** Implement login flow (server setup + TOTP).

**Tasks:**

1. **KeychainService.swift** - Secure token storage
   ```swift
   class KeychainService {
       static func save(token: String) { /* Use Security framework */ }
       static func getToken() -> String? { /* Retrieve from Keychain */ }
       static func deleteToken() { /* Remove from Keychain */ }
   }
   ```

2. **APIService.swift** - REST API calls
   ```swift
   class APIService {
       static let shared = APIService()
       var baseURL: String = ""

       func verifyTOTP(code: String) async throws -> AuthResponse {
           // POST /api/v1/auth/totp/verify
       }

       func getSession() async throws -> Session {
           // GET /api/v1/sessions
       }

       func createSession(workingDir: String, autoStart: Bool) async throws -> Session {
           // POST /api/v1/sessions
       }
   }
   ```

3. **ServerSetupView.swift** - URL input
   ```swift
   struct ServerSetupView: View {
       @State var serverURL = ""

       var body: some View {
           VStack {
               TextField("Server URL", text: $serverURL)
               Button("Continue") {
                   UserDefaults.standard.set(serverURL, forKey: "serverURL")
                   APIService.shared.baseURL = serverURL
                   // Navigate to TOTP
               }
           }
       }
   }
   ```

4. **TOTPLoginView.swift** - 6-digit code input
   ```swift
   struct TOTPLoginView: View {
       @State var code = ""
       @EnvironmentObject var appState: AppState

       var body: some View {
           VStack {
               TextField("6-digit code", text: $code)
                   .keyboardType(.numberPad)
               Button("Login") {
                   Task {
                       let response = try await APIService.shared.verifyTOTP(code: code)
                       KeychainService.save(token: response.token)
                       appState.isAuthenticated = true
                   }
               }
           }
       }
   }
   ```

5. **AuthService.swift** - JWT validation
   ```swift
   class AuthService {
       static func isTokenExpired(_ token: String) -> Bool {
           // Decode JWT, check exp claim
       }
   }
   ```

**Deliverable:** User can enter server URL, enter TOTP code, login, token stored in Keychain.

**Test:** Mock server or use real Cloude Code server on Mac.

---

### **Phase 3: Launchpad** (Day 3)

**Goal:** Project list, create/delete projects.

**Tasks:**

1. **Models/Project.swift**
   ```swift
   struct Project: Codable, Identifiable {
       let id = UUID()
       let name: String
       let path: String
       let description: String?
   }
   ```

2. **APIService.swift** - Project endpoints
   ```swift
   func getProjects() async throws -> [Project] {
       // GET /api/v1/projects
   }

   func createProject(name: String, path: String, description: String?) async throws -> Project {
       // POST /api/v1/projects
   }

   func deleteProject(name: String) async throws {
       // DELETE /api/v1/projects/{name}
   }
   ```

3. **LaunchpadView.swift** - Project list
   ```swift
   struct LaunchpadView: View {
       @State var projects: [Project] = []
       @State var showNewProjectSheet = false

       var body: some View {
           NavigationView {
               List {
                   ForEach(projects) { project in
                       ProjectRow(project: project)
                           .onTapGesture {
                               createSessionForProject(project)
                           }
                   }
                   .onDelete { indexSet in
                       deleteProjects(at: indexSet)
                   }
               }
               .refreshable {
                   await loadProjects()
               }
               .toolbar {
                   Button("New") { showNewProjectSheet = true }
               }
               .sheet(isPresented: $showNewProjectSheet) {
                   NewProjectSheet()
               }
           }
           .onAppear {
               Task { await loadProjects() }
           }
       }

       func createSessionForProject(_ project: Project) {
           Task {
               let session = try await APIService.shared.createSession(
                   workingDir: project.path,
                   autoStart: true
               )
               appState.currentSession = session
           }
       }
   }
   ```

4. **ProjectRow.swift** - List item
   ```swift
   struct ProjectRow: View {
       let project: Project

       var body: some View {
           VStack(alignment: .leading) {
               Text(project.name).font(.headline)
               Text(project.path).font(.caption).foregroundColor(.secondary)
               if let desc = project.description {
                   Text(desc).font(.subheadline)
               }
           }
       }
   }
   ```

5. **NewProjectSheet.swift** - Create modal
   ```swift
   struct NewProjectSheet: View {
       @State var name = ""
       @State var description = ""
       @Environment(\.dismiss) var dismiss

       var body: some View {
           NavigationView {
               Form {
                   TextField("Project Name", text: $name)
                   TextField("Description (optional)", text: $description)
               }
               .navigationTitle("New Project")
               .toolbar {
                   Button("Create") {
                       Task {
                           // Create project
                           dismiss()
                       }
                   }
                   Button("Cancel") { dismiss() }
               }
           }
       }
   }
   ```

**Deliverable:** List projects, tap to create session, swipe to delete, pull to refresh.

---

### **Phase 4: Terminal Core** (Day 4-5)

**Goal:** Integrate SwiftTerm, display terminal (no WebSocket yet).

**Tasks:**

1. **TerminalViewController.swift** - UIKit controller
   ```swift
   import SwiftTerm
   import UIKit

   class TerminalViewController: UIViewController, TerminalViewDelegate {
       var terminalView: TerminalView!
       var onSendData: ((Data) -> Void)?
       var onResize: ((Int, Int) -> Void)?

       override func viewDidLoad() {
           super.viewDidLoad()

           terminalView = TerminalView(frame: view.bounds)
           terminalView.terminalDelegate = self
           terminalView.autoresizingMask = [.flexibleWidth, .flexibleHeight]
           view.addSubview(terminalView)
       }

       func send(source: TerminalView, data: ArraySlice<UInt8>) {
           onSendData?(Data(data))
       }

       func sizeChanged(source: TerminalView, newCols: Int, newRows: Int) {
           onResize?(newCols, newRows)
       }

       func feedTerminal(data: Data) {
           terminalView.feed(byteArray: [UInt8](data))
       }
   }
   ```

2. **TerminalContainerView.swift** - SwiftUI wrapper
   ```swift
   struct TerminalContainerView: UIViewControllerRepresentable {
       let session: Session

       func makeUIViewController(context: Context) -> TerminalViewController {
           let controller = TerminalViewController()

           // Test with hardcoded output
           let testOutput = "Welcome to Cloude Code!\n$ "
           controller.feedTerminal(data: testOutput.data(using: .utf8)!)

           return controller
       }

       func updateUIViewController(_ uiViewController: TerminalViewController, context: Context) {}
   }
   ```

3. **Test ANSI colors**
   ```swift
   let colorTest = """
   \u{1b}[31mRed\u{1b}[0m
   \u{1b}[32mGreen\u{1b}[0m
   \u{1b}[34mBlue\u{1b}[0m
   \u{1b}[1mBold\u{1b}[0m
   """
   controller.feedTerminal(data: colorTest.data(using: .utf8)!)
   ```

4. **Add keyboard accessory bar**
   ```swift
   // See "Keyboard Handling" section above
   ```

**Deliverable:** Terminal displays, renders ANSI colors, keyboard accessory bar works.

**Test:** Hardcoded output displays correctly.

---

### **Phase 5: WebSocket Integration** (Day 6-7)

**Goal:** Connect terminal to Cloude Code server via WebSocket.

**Tasks:**

1. **WebSocketService.swift** - WebSocket manager
   ```swift
   import Foundation
   import Combine

   class WebSocketService: ObservableObject {
       private var webSocket: URLSessionWebSocketTask?
       @Published var isConnected = false
       @Published var reconnectAttempt = 0

       var onBinaryMessage: ((Data) -> Void)?
       var onTunnelCreated: ((Tunnel) -> Void)?
       var onLogMessage: ((String) -> Void)?

       func connect(serverURL: String, token: String) {
           let wsURL = serverURL.replacingOccurrences(of: "http", with: "ws")
           let url = URL(string: "\(wsURL)/ws/terminal?token=\(token)")!

           webSocket = URLSession.shared.webSocketTask(with: url)
           webSocket?.resume()
           isConnected = true
           reconnectAttempt = 0

           receiveMessage()
           startKeepalive()
       }

       func disconnect() {
           webSocket?.cancel(with: .goingAway, reason: nil)
           isConnected = false
       }

       func send(data: Data) {
           webSocket?.send(.data(data)) { error in
               if let error = error {
                   print("WebSocket send error: \(error)")
               }
           }
       }

       func sendResize(cols: Int, rows: Int) {
           let message = "{\"type\":\"pty_resize\",\"cols\":\(cols),\"rows\":\(rows)}"
           webSocket?.send(.string(message)) { _ in }
       }

       private func receiveMessage() {
           webSocket?.receive { [weak self] result in
               switch result {
               case .success(let message):
                   switch message {
                   case .data(let data):
                       // PTY output (binary)
                       self?.onBinaryMessage?(data)
                   case .string(let text):
                       // JSON messages (tunnels, logs, pong)
                       self?.handleJSONMessage(text)
                   @unknown default:
                       break
                   }
                   // Continue receiving
                   self?.receiveMessage()

               case .failure(let error):
                   print("WebSocket receive error: \(error)")
                   self?.isConnected = false
                   self?.attemptReconnect()
               }
           }
       }

       private func handleJSONMessage(_ text: String) {
           guard let data = text.data(using: .utf8),
                 let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                 let type = json["type"] as? String else {
               return
           }

           switch type {
           case "tunnel_created":
               // Parse tunnel object, call onTunnelCreated
               break
           case "log":
               if let content = json["content"] as? String {
                   onLogMessage?(content)
               }
           case "pong":
               // Keepalive response
               break
           default:
               break
           }
       }

       private func startKeepalive() {
           Timer.scheduledTimer(withTimeInterval: 30, repeats: true) { [weak self] _ in
               self?.webSocket?.send(.string("{\"type\":\"ping\"}")) { _ in }
           }
       }

       private func attemptReconnect() {
           reconnectAttempt += 1
           guard reconnectAttempt <= 5 else { return }

           let delay = min(pow(2.0, Double(reconnectAttempt - 1)), 16.0)
           DispatchQueue.main.asyncAfter(deadline: .now() + delay) { [weak self] in
               guard let self = self,
                     let serverURL = UserDefaults.standard.string(forKey: "serverURL"),
                     let token = KeychainService.getToken() else {
                   return
               }
               self.connect(serverURL: serverURL, token: token)
           }
       }
   }
   ```

2. **Update TerminalContainerView.swift**
   ```swift
   struct TerminalContainerView: UIViewControllerRepresentable {
       @ObservedObject var webSocketService: WebSocketService
       let session: Session

       func makeUIViewController(context: Context) -> TerminalViewController {
           let controller = TerminalViewController()

           // Terminal input → WebSocket
           controller.onSendData = { data in
               webSocketService.send(data: data)
           }

           // Terminal resize → WebSocket
           controller.onResize = { cols, rows in
               webSocketService.sendResize(cols: cols, rows: rows)
           }

           // WebSocket output → Terminal
           webSocketService.onBinaryMessage = { data in
               DispatchQueue.main.async {
                   controller.feedTerminal(data: data)
               }
           }

           return controller
       }

       func updateUIViewController(_ uiViewController: TerminalViewController, context: Context) {}
   }
   ```

3. **Connect on terminal appear**
   ```swift
   struct TerminalView: View {
       @StateObject var webSocketService = WebSocketService()
       let session: Session

       var body: some View {
           VStack {
               if webSocketService.isConnected {
                   TerminalContainerView(webSocketService: webSocketService, session: session)
               } else if webSocketService.reconnectAttempt > 0 {
                   Text("Reconnecting... (\(webSocketService.reconnectAttempt)/5)")
               } else {
                   ProgressView("Connecting...")
               }
           }
           .onAppear {
               guard let serverURL = UserDefaults.standard.string(forKey: "serverURL"),
                     let token = KeychainService.getToken() else {
                   return
               }
               webSocketService.connect(serverURL: serverURL, token: token)
           }
           .onDisappear {
               webSocketService.disconnect()
           }
       }
   }
   ```

**Deliverable:** Terminal connects to server, displays real PTY output, accepts user input.

**Test:**
1. Run Cloude Code server on Mac
2. Launch iOS app (simulator or device on same WiFi)
3. Enter server URL: `http://YOUR_MAC_IP:8000`
4. Login with TOTP
5. Create/select project
6. Terminal should show Claude Code prompt
7. Type commands, verify they execute

---

### **Phase 6: Features** (Day 8-10)

**Goal:** Add tunnels, better UX, error handling.

**Tasks:**

1. **TunnelBarView.swift** - Show active tunnels
   ```swift
   struct TunnelBarView: View {
       let tunnels: [Tunnel]

       var body: some View {
           if !tunnels.isEmpty {
               ScrollView(.horizontal, showsIndicators: false) {
                   HStack {
                       ForEach(tunnels) { tunnel in
                           TunnelButton(tunnel: tunnel)
                       }
                   }
                   .padding(.horizontal)
               }
               .frame(height: 44)
               .background(Color.blue.opacity(0.1))
           }
       }
   }

   struct TunnelButton: View {
       let tunnel: Tunnel

       var body: some View {
           Button {
               UIPasteboard.general.string = tunnel.publicURL
               // Show toast: "URL copied"
           } label: {
               HStack {
                   Text(":\(tunnel.port)")
                   Image(systemName: "doc.on.doc")
               }
               .padding(.horizontal, 12)
               .padding(.vertical, 6)
               .background(Color.blue)
               .foregroundColor(.white)
               .cornerRadius(8)
           }
       }
   }
   ```

2. **Handle tunnel_created WebSocket messages**
   ```swift
   // In WebSocketService.handleJSONMessage()
   case "tunnel_created":
       if let tunnelData = try? JSONSerialization.data(withJSONObject: json["tunnel"] as Any),
          let tunnel = try? JSONDecoder().decode(Tunnel.self, from: tunnelData) {
           onTunnelCreated?(tunnel)
       }

   // In TerminalView
   @State var tunnels: [Tunnel] = []

   webSocketService.onTunnelCreated = { tunnel in
       tunnels.append(tunnel)
   }

   var body: some View {
       VStack(spacing: 0) {
           TunnelBarView(tunnels: tunnels)
           TerminalContainerView(...)
       }
   }
   ```

3. **Session conflict resolution**
   ```swift
   // In LaunchpadView.createSessionForProject()
   do {
       let session = try await APIService.shared.createSession(...)
       appState.currentSession = session
   } catch {
       if error.localizedDescription.contains("already running") {
           // Show alert
           showSessionConflictAlert = true
       }
   }

   // Alert with options
   .alert("Session Already Running", isPresented: $showSessionConflictAlert) {
       Button("Connect") {
           // GET /api/v1/sessions
       }
       Button("Destroy & Create New") {
           // DELETE /api/v1/sessions
           // POST /api/v1/sessions
       }
       Button("Cancel", role: .cancel) {}
   }
   ```

4. **Reconnection logic improvements**
   - Show reconnect status in terminal
   - Display countdown timer
   - Manual retry button after max attempts

5. **Loading states**
   ```swift
   struct LaunchpadView: View {
       @State var isLoading = false
       @State var error: Error?

       var body: some View {
           if isLoading {
               ProgressView("Creating session...")
           } else if let error = error {
               ErrorView(error: error) {
                   // Retry button
               }
           } else {
               // Project list
           }
       }
   }
   ```

**Deliverable:** Tunnels displayed, tap to copy URL, session conflicts handled, error states polished.

---

### **Phase 7: Polish** (Day 11-14)

**Goal:** Production-ready quality.

**Tasks:**

1. **Orientation changes**
   - Test landscape/portrait transitions
   - Ensure terminal resizes correctly
   - Send resize events to server

2. **iPad external keyboard**
   - Test all key combinations
   - Add keyboard shortcuts (Cmd+K, etc.)
   - Handle iPad-specific safe areas

3. **Haptic feedback**
   ```swift
   // In KeyboardAccessoryView button taps
   let impact = UIImpactFeedbackGenerator(style: .light)
   impact.impactOccurred()
   ```

4. **Error messages**
   - Network errors: "Could not connect to server. Check WiFi."
   - Auth errors: "Invalid TOTP code. Try again."
   - Session errors: "Session creation failed. Try again."

5. **App icon**
   - Design icon (cloud + code symbol)
   - Export @1x, @2x, @3x for all sizes
   - Add to Assets.xcassets

6. **Dark mode**
   - Test terminal colors in dark mode
   - Ensure UI elements adapt
   - SwiftTerm should handle this automatically

7. **Landscape layout**
   - iPhone landscape: full-screen terminal
   - iPad landscape: consider split-view options

8. **Settings screen polish**
   ```swift
   struct SettingsView: View {
       var body: some View {
           Form {
               Section("Server") {
                   Text(UserDefaults.standard.string(forKey: "serverURL") ?? "")
                   Button("Change Server") { /* ... */ }
               }

               Section("Authentication") {
                   Button("Logout") {
                       KeychainService.deleteToken()
                       // Navigate to login
                   }
               }

               Section("About") {
                   HStack {
                       Text("Version")
                       Spacer()
                       Text(Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "1.0")
                   }
               }
           }
           .navigationTitle("Settings")
       }
   }
   ```

9. **TestFlight setup**
   - Archive build in Xcode
   - Upload to App Store Connect
   - Create TestFlight beta
   - Invite testers
   - Gather feedback

10. **Documentation**
    ```markdown
    # Cloude Code iOS App

    ## Setup
    1. Run Cloude Code server on your Mac
    2. Find your Mac's IP: `ifconfig | grep inet`
    3. On iOS app, enter server URL: `http://YOUR_IP:8000`
    4. Enter TOTP code from Google Authenticator
    5. Create/select project
    6. Start coding!

    ## Requirements
    - iOS 15.0+
    - Cloude Code server running on Mac
    - Same WiFi network (or VPN/tunnel)

    ## Features
    - Native terminal emulation (SwiftTerm)
    - Full ANSI color support
    - Hardware keyboard support
    - Auto-tunneling (tap to copy URLs)
    - Session persistence
    - Auto-reconnect
    ```

**Deliverable:** Production-ready app, submitted to TestFlight, ready for beta testing.

---

## Code Samples

### Complete WebSocketService

```swift
import Foundation
import Combine

class WebSocketService: ObservableObject {
    private var webSocket: URLSessionWebSocketTask?
    private var keepaliveTimer: Timer?

    @Published var isConnected = false
    @Published var reconnectAttempt = 0
    @Published var connectionError: Error?

    var onBinaryMessage: ((Data) -> Void)?
    var onTunnelCreated: ((Tunnel) -> Void)?
    var onLogMessage: ((String) -> Void)?

    func connect(serverURL: String, token: String) {
        // Convert http:// to ws://
        let wsURL = serverURL
            .replacingOccurrences(of: "http://", with: "ws://")
            .replacingOccurrences(of: "https://", with: "wss://")

        guard let url = URL(string: "\(wsURL)/ws/terminal?token=\(token)") else {
            connectionError = NSError(domain: "Invalid URL", code: -1)
            return
        }

        webSocket = URLSession.shared.webSocketTask(with: url)
        webSocket?.resume()

        isConnected = true
        reconnectAttempt = 0
        connectionError = nil

        receiveMessage()
        startKeepalive()
    }

    func disconnect() {
        keepaliveTimer?.invalidate()
        keepaliveTimer = nil

        webSocket?.cancel(with: .goingAway, reason: nil)
        webSocket = nil

        isConnected = false
    }

    func send(data: Data) {
        webSocket?.send(.data(data)) { [weak self] error in
            if let error = error {
                print("WebSocket send error: \(error)")
                self?.connectionError = error
            }
        }
    }

    func sendResize(cols: Int, rows: Int) {
        let message = """
        {"type":"pty_resize","cols":\(cols),"rows":\(rows)}
        """

        webSocket?.send(.string(message)) { error in
            if let error = error {
                print("WebSocket resize send error: \(error)")
            }
        }
    }

    private func receiveMessage() {
        webSocket?.receive { [weak self] result in
            guard let self = self else { return }

            switch result {
            case .success(let message):
                switch message {
                case .data(let data):
                    // PTY output (binary WebSocket frame)
                    DispatchQueue.main.async {
                        self.onBinaryMessage?(data)
                    }

                case .string(let text):
                    // JSON messages (tunnels, logs, pong)
                    self.handleJSONMessage(text)

                @unknown default:
                    break
                }

                // Continue receiving
                self.receiveMessage()

            case .failure(let error):
                print("WebSocket receive error: \(error)")
                DispatchQueue.main.async {
                    self.isConnected = false
                    self.connectionError = error
                    self.attemptReconnect()
                }
            }
        }
    }

    private func handleJSONMessage(_ text: String) {
        guard let data = text.data(using: .utf8),
              let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let type = json["type"] as? String else {
            return
        }

        switch type {
        case "tunnel_created":
            if let tunnelDict = json["tunnel"] as? [String: Any],
               let tunnelData = try? JSONSerialization.data(withJSONObject: tunnelDict),
               let tunnel = try? JSONDecoder().decode(Tunnel.self, from: tunnelData) {
                DispatchQueue.main.async {
                    self.onTunnelCreated?(tunnel)
                }
            }

        case "log":
            if let content = json["content"] as? String {
                DispatchQueue.main.async {
                    self.onLogMessage?(content)
                }
            }

        case "pong":
            // Keepalive response - no action needed
            break

        case "error":
            if let errorMsg = json["message"] as? String {
                print("Server error: \(errorMsg)")
            }

        default:
            print("Unknown message type: \(type)")
        }
    }

    private func startKeepalive() {
        // Send ping every 30 seconds
        keepaliveTimer = Timer.scheduledTimer(withTimeInterval: 30, repeats: true) { [weak self] _ in
            self?.webSocket?.send(.string("{\"type\":\"ping\"}")) { error in
                if let error = error {
                    print("Keepalive ping error: \(error)")
                }
            }
        }
    }

    private func attemptReconnect() {
        reconnectAttempt += 1

        guard reconnectAttempt <= 5 else {
            print("Max reconnect attempts reached")
            return
        }

        // Exponential backoff: 1s, 2s, 4s, 8s, 16s
        let delay = min(pow(2.0, Double(reconnectAttempt - 1)), 16.0)

        print("Reconnecting in \(delay) seconds... (attempt \(reconnectAttempt)/5)")

        DispatchQueue.main.asyncAfter(deadline: .now() + delay) { [weak self] in
            guard let self = self,
                  let serverURL = UserDefaults.standard.string(forKey: "serverURL"),
                  let token = KeychainService.getToken() else {
                print("Cannot reconnect: missing server URL or token")
                return
            }

            print("Attempting to reconnect...")
            self.connect(serverURL: serverURL, token: token)
        }
    }
}
```

### Complete TerminalViewController

```swift
import SwiftTerm
import UIKit

class TerminalViewController: UIViewController, TerminalViewDelegate {
    var terminalView: TerminalView!
    var keyboardAccessory: KeyboardAccessoryView!

    var onSendData: ((Data) -> Void)?
    var onResize: ((Int, Int) -> Void)?

    override func viewDidLoad() {
        super.viewDidLoad()

        setupTerminal()
        setupKeyboardAccessory()
    }

    private func setupTerminal() {
        terminalView = TerminalView(frame: view.bounds)
        terminalView.terminalDelegate = self
        terminalView.autoresizingMask = [.flexibleWidth, .flexibleHeight]

        // Configure terminal options
        terminalView.getTerminal().silentLog = false

        view.addSubview(terminalView)
    }

    private func setupKeyboardAccessory() {
        keyboardAccessory = KeyboardAccessoryView()
        keyboardAccessory.onKeyPress = { [weak self] code in
            self?.terminalView.send(text: code)
        }
    }

    // MARK: - TerminalViewDelegate

    func send(source: TerminalView, data: ArraySlice<UInt8>) {
        // User typed in terminal - send to WebSocket
        onSendData?(Data(data))
    }

    func sizeChanged(source: TerminalView, newCols: Int, newRows: Int) {
        // Terminal resized - notify server
        onResize?(newCols, newRows)
    }

    // MARK: - Public Methods

    func feedTerminal(data: Data) {
        // Receive data from WebSocket - feed to terminal
        terminalView.feed(byteArray: [UInt8](data))
    }

    // MARK: - Keyboard Accessory

    override var inputAccessoryView: UIView? {
        return keyboardAccessory
    }

    override var canBecomeFirstResponder: Bool {
        return true
    }
}

class KeyboardAccessoryView: UIView {
    var onKeyPress: ((String) -> Void)?

    init() {
        super.init(frame: CGRect(x: 0, y: 0, width: UIScreen.main.bounds.width, height: 44))
        backgroundColor = .systemGray6

        setupButtons()
    }

    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    private func setupButtons() {
        let keys: [(String, String)] = [
            ("Esc", "\u{1b}"),           // Escape
            ("Tab", "\t"),                // Tab
            ("^C", "\u{03}"),            // Ctrl+C
            ("^D", "\u{04}"),            // Ctrl+D
            ("↑", "\u{1b}[A"),           // Up arrow
            ("↓", "\u{1b}[B"),           // Down arrow
            ("←", "\u{1b}[D"),           // Left arrow
            ("→", "\u{1b}[C")            // Right arrow
        ]

        let stackView = UIStackView()
        stackView.axis = .horizontal
        stackView.distribution = .fillEqually
        stackView.spacing = 4
        stackView.translatesAutoresizingMaskIntoConstraints = false

        for (label, code) in keys {
            let button = UIButton(type: .system)
            button.setTitle(label, for: .normal)
            button.titleLabel?.font = .systemFont(ofSize: 14, weight: .medium)
            button.backgroundColor = .systemGray5
            button.layer.cornerRadius = 4

            button.addAction(UIAction { [weak self] _ in
                let impact = UIImpactFeedbackGenerator(style: .light)
                impact.impactOccurred()
                self?.onKeyPress?(code)
            }, for: .touchUpInside)

            stackView.addArrangedSubview(button)
        }

        addSubview(stackView)

        NSLayoutConstraint.activate([
            stackView.leadingAnchor.constraint(equalTo: leadingAnchor, constant: 8),
            stackView.trailingAnchor.constraint(equalTo: trailingAnchor, constant: -8),
            stackView.topAnchor.constraint(equalTo: topAnchor, constant: 4),
            stackView.bottomAnchor.constraint(equalTo: bottomAnchor, constant: -4)
        ])
    }
}
```

### Complete Models

```swift
import Foundation

// Session
struct Session: Codable, Identifiable {
    let id: String
    let workingDir: String
    let status: String
    let createdAt: String
    let ptyPid: Int?

    enum CodingKeys: String, CodingKey {
        case id
        case workingDir = "working_dir"
        case status
        case createdAt = "created_at"
        case ptyPid = "pty_pid"
    }
}

// Project
struct Project: Codable, Identifiable {
    let id = UUID()
    let name: String
    let path: String
    let description: String?

    enum CodingKeys: String, CodingKey {
        case name, path, description
    }
}

// Tunnel
struct Tunnel: Codable, Identifiable {
    let id: String
    let sessionId: String
    let port: Int
    let publicURL: String
    let status: String
    let processPid: Int?

    enum CodingKeys: String, CodingKey {
        case id
        case sessionId = "session_id"
        case port
        case publicURL = "public_url"
        case status
        case processPid = "process_pid"
    }
}

// Auth Response
struct AuthResponse: Codable {
    let token: String
    let expiresIn: Int

    enum CodingKeys: String, CodingKey {
        case token
        case expiresIn = "expires_in"
    }
}
```

---

## Benefits Over Web UI

1. **Native Performance**
   - No WebView overhead
   - Direct SwiftTerm rendering (GPU-accelerated)
   - Lower memory usage

2. **Better Keyboard Support**
   - Hardware keyboard fully supported
   - Custom keyboard accessory bar
   - iPadOS keyboard shortcuts (Cmd+K, etc.)

3. **iOS Integration**
   - Home screen icon
   - Multitasking (split-screen on iPad)
   - Handoff (start on iPhone, continue on iPad)
   - Background refresh (could add later)

4. **Offline-Capable**
   - Better state persistence (UserDefaults, Keychain)
   - Cached project list
   - Reconnect automatically

5. **Native Gestures**
   - Swipe navigation
   - Pull-to-refresh
   - Long-press context menus

6. **App Store Distribution**
   - Easier discovery
   - Automatic updates
   - TestFlight beta testing

7. **Push Notifications** (Future)
   - Server alerts ("Build failed", "Deployment complete")
   - Tunnel created notifications

8. **Better Mobile Experience**
   - Native scroll physics
   - Haptic feedback
   - iOS-native alerts/sheets

---

## Challenges & Solutions

### Challenge: Binary WebSocket in Swift
**Solution:** URLSession WebSocket natively supports `.data` messages (iOS 13+). No third-party libraries needed.

### Challenge: SwiftTerm is UIKit, app is SwiftUI
**Solution:** `UIViewControllerRepresentable` wrapper - standard SwiftUI pattern for wrapping UIKit views.

### Challenge: iOS suspends apps (WebSocket disconnects)
**Solution:**
- Auto-reconnect on app resume with exponential backoff
- Server session persists (PTY keeps running)
- Show "Reconnecting..." indicator

### Challenge: iOS keyboards lack terminal keys
**Solution:**
- Custom keyboard accessory bar with Esc, Tab, Ctrl+C, arrows
- Hardware keyboard fully supported on iPad

### Challenge: JWT expiry (30 minutes default)
**Solution:**
- Handle 401 responses from API
- Clear Keychain token
- Redirect to login screen
- User re-authenticates with TOTP

### Challenge: Testing on real device
**Solution:**
- Mac and iOS device on same WiFi network
- Use Mac's local IP: `ifconfig | grep inet` (e.g., 192.168.1.100)
- Server URL: `http://192.168.1.100:8000`

### Challenge: Xcode simulator limitations
**Solution:**
- Simulator can connect to localhost:8000 (Mac's localhost)
- Real device requires Mac's IP address
- Use Xcode → Window → Devices to install on real device

---

## Timeline

### **2 Weeks Total**

**Week 1: Foundation**
- Day 1: Project setup, dependencies
- Day 2: Authentication (server setup, TOTP, JWT)
- Day 3: Launchpad (project list, create/delete)
- Day 4-5: Terminal core (SwiftTerm integration)

**Week 2: Integration & Polish**
- Day 6-7: WebSocket integration (terminal I/O, resize)
- Day 8-10: Features (tunnels, error handling, reconnect)
- Day 11-14: Polish (iPad, keyboard, icons, TestFlight)

### **Milestones**

**End of Day 2:** User can login with TOTP
**End of Day 3:** User can see project list
**End of Day 5:** Terminal displays (hardcoded output)
**End of Day 7:** Terminal connects to server, bidirectional I/O works
**End of Day 10:** Full feature parity with web UI
**End of Day 14:** Production-ready, in TestFlight

---

## Deliverables

1. **Xcode Project**
   - Location: `./ios/CloudeCode/CloudeCode.xcodeproj`
   - Swift, SwiftUI, iOS 15+
   - SwiftTerm dependency via SPM

2. **Working iOS App**
   - `.ipa` for TestFlight distribution
   - App Store Connect submission
   - TestFlight beta invite link

3. **Documentation**
   - `./ios/README.md` - Setup instructions
   - In-app help screen (optional)
   - TestFlight release notes

4. **Optional: App Store Submission**
   - App Store listing (screenshots, description)
   - Privacy policy (if required)
   - App review submission

---

## Next Steps

1. **Create Xcode project in `./ios/` folder**
2. **Follow implementation phases sequentially**
3. **Test on real device early** (WiFi connectivity)
4. **Iterate based on TestFlight feedback**
5. **Submit to App Store** (optional)

---

## Resources

**SwiftTerm:**
- Main repo: https://github.com/migueldeicaza/SwiftTerm
- Docs: https://migueldeicaza.github.io/SwiftTermDocs/documentation/swiftterm/
- Example app: https://github.com/migueldeicaza/SwiftTermApp

**Commercial Apps Using SwiftTerm:**
- La Terminal: https://la-terminal.net
- Secure Shellfish: https://secureshellfish.app
- CodeEdit: https://www.codeedit.app

**Apple Documentation:**
- URLSession WebSocket: https://developer.apple.com/documentation/foundation/urlsessionwebsockettask
- Keychain Services: https://developer.apple.com/documentation/security/keychain_services
- UIViewControllerRepresentable: https://developer.apple.com/documentation/swiftui/uiviewcontrollerrepresentable

**Miguel de Icaza:**
- Blog: https://tirania.org/blog/
- Twitter: @migueldeicaza
- Built: Xamarin, Mono, Midnight Commander

---

## Notes

- SwiftTerm is **MIT licensed** - use it however you want (even commercial apps)
- URLSession WebSocket is **built into iOS 13+** - no dependencies needed
- The FastAPI backend **requires NO changes** - iOS app is just another client
- **Same WiFi network required** unless you setup VPN/tunnel to access Mac remotely
- App Store submission **optional** - TestFlight is sufficient for personal use
- Consider adding **app icon** early for better branding
- **iPad support** is basically free - SwiftUI scales automatically

---

**Last Updated:** 2025-11-19
**Status:** Ready to implement
**Estimated Effort:** 2 weeks (solo developer)
