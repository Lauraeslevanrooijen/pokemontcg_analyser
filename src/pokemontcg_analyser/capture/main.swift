// Screen recorder for Pokémon TCG Live sessions, built on ScreenCaptureKit.
//
// ffmpeg's avfoundation input only gets ~12 frames a second from macOS while
// a fullscreen game is in front (measured; 30 with any other app in front),
// which is what made recordings stutter. ScreenCaptureKit is not held back
// that way, scales on the GPU and writes the MP4 directly.
//
// Usage: ptcg-capture --output file.mp4 [--max-width 1920] [--fps 30]
//                     [--crop x,y,w,h] [--microphone "Device name"] [--no-cursor]
// --crop is in fractions of the main display. Stop with "q" on stdin or
// Ctrl+C; the file is finalized before the process exits.

import AVFoundation
import CoreMedia
import Foundation
import ScreenCaptureKit

struct Options {
    var output = ""
    var maxWidth = 1920
    var fps = 30
    var crop: CGRect? = nil
    var microphone: String? = nil
    var showCursor = true
}

func fail(_ message: String) -> Never {
    FileHandle.standardError.write("error: \(message)\n".data(using: .utf8)!)
    exit(1)
}

func log(_ message: String) {
    FileHandle.standardError.write("\(message)\n".data(using: .utf8)!)
}

func parseOptions() -> Options {
    var options = Options()
    var args = Array(CommandLine.arguments.dropFirst())
    func value(_ flag: String) -> String {
        guard !args.isEmpty else { fail("\(flag) needs a value") }
        return args.removeFirst()
    }
    while !args.isEmpty {
        let flag = args.removeFirst()
        switch flag {
        case "--output": options.output = value(flag)
        case "--max-width": options.maxWidth = Int(value(flag)) ?? 1920
        case "--fps": options.fps = Int(value(flag)) ?? 30
        case "--microphone": options.microphone = value(flag)
        case "--no-cursor": options.showCursor = false
        case "--crop":
            let parts = value(flag).split(separator: ",").compactMap { Double($0) }
            guard parts.count == 4 else { fail("--crop takes x,y,w,h") }
            options.crop = CGRect(x: parts[0], y: parts[1], width: parts[2], height: parts[3])
        default: fail("unknown option \(flag)")
        }
    }
    if options.output.isEmpty { fail("--output is required") }
    return options
}

final class Recorder: NSObject, SCStreamOutput, SCStreamDelegate {
    private let options: Options
    private let queue = DispatchQueue(label: "ptcg-capture")
    private var stream: SCStream?
    private var writer: AVAssetWriter!
    private var videoInput: AVAssetWriterInput!
    private var adaptor: AVAssetWriterInputPixelBufferAdaptor!
    private var audioInput: AVAssetWriterInput?
    private var timer: DispatchSourceTimer?

    private var latest: CVPixelBuffer?
    private var start = CMTime.invalid
    private var lastTick: Int64 = -1
    private var delivered = 0
    private var written = 0
    private var stopping = false

    init(options: Options) {
        self.options = options
    }

    func run() async throws {
        let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false)
        guard let display = content.displays.first(where: { $0.displayID == CGMainDisplayID() })
            ?? content.displays.first
        else { fail("no display to record") }

        let filter = SCContentFilter(display: display, excludingWindows: [])
        let config = SCStreamConfiguration()
        // The display's size is in points; the picture is captured at
        // pixel resolution and scaled down on the GPU to the output size.
        var source = CGRect(x: 0, y: 0, width: CGFloat(display.width), height: CGFloat(display.height))
        if let crop = options.crop {
            source = CGRect(
                x: crop.minX * source.width, y: crop.minY * source.height,
                width: crop.width * source.width, height: crop.height * source.height
            ).integral
            config.sourceRect = source
        }
        let pixelWidth = Int(source.width * CGFloat(filter.pointPixelScale))
        let width = min(options.maxWidth, pixelWidth) / 2 * 2
        let height = Int((Double(width) * Double(source.height) / Double(source.width)).rounded()) / 2 * 2
        config.width = width
        config.height = height
        config.minimumFrameInterval = CMTime(value: 1, timescale: CMTimeScale(options.fps))
        config.pixelFormat = kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange
        config.showsCursor = options.showCursor
        config.queueDepth = 6

        if let name = options.microphone {
            let devices = AVCaptureDevice.DiscoverySession(
                deviceTypes: [.microphone, .external], mediaType: .audio, position: .unspecified
            ).devices
            guard let device = devices.first(where: { $0.localizedName == name }) else {
                fail("microphone not found: \(name)")
            }
            config.captureMicrophone = true
            config.microphoneCaptureDeviceID = device.uniqueID
        }

        let url = URL(fileURLWithPath: options.output)
        try? FileManager.default.removeItem(at: url)
        writer = try AVAssetWriter(outputURL: url, fileType: .mp4)
        // Puts the index at the front, so a browser can play and seek
        // without fetching the whole file first.
        writer.shouldOptimizeForNetworkUse = true
        videoInput = AVAssetWriterInput(mediaType: .video, outputSettings: [
            AVVideoCodecKey: AVVideoCodecType.h264,
            AVVideoWidthKey: width,
            AVVideoHeightKey: height,
            AVVideoCompressionPropertiesKey: [
                AVVideoAverageBitRateKey: 4_000_000,
                AVVideoExpectedSourceFrameRateKey: options.fps,
                // A keyframe at least every 5s keeps seeking precise in the
                // browser without a large frame to decode every second.
                AVVideoMaxKeyFrameIntervalDurationKey: 5,
                AVVideoAllowFrameReorderingKey: false,
                AVVideoProfileLevelKey: AVVideoProfileLevelH264HighAutoLevel,
            ] as [String: Any],
        ])
        videoInput.expectsMediaDataInRealTime = true
        adaptor = AVAssetWriterInputPixelBufferAdaptor(assetWriterInput: videoInput, sourcePixelBufferAttributes: nil)
        writer.add(videoInput)
        if options.microphone != nil {
            let input = AVAssetWriterInput(mediaType: .audio, outputSettings: [
                AVFormatIDKey: kAudioFormatMPEG4AAC,
                AVSampleRateKey: 48_000,
                AVNumberOfChannelsKey: 1,
                AVEncoderBitRateKey: 128_000,
            ])
            input.expectsMediaDataInRealTime = true
            writer.add(input)
            audioInput = input
        }

        let stream = SCStream(filter: filter, configuration: config, delegate: self)
        try stream.addStreamOutput(self, type: .screen, sampleHandlerQueue: queue)
        if options.microphone != nil {
            try stream.addStreamOutput(self, type: .microphone, sampleHandlerQueue: queue)
        }
        self.stream = stream
        try await stream.startCapture()
        log("recording \(width)x\(height) at \(options.fps) fps")
    }

    func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer, of type: SCStreamOutputType) {
        guard !stopping, sampleBuffer.isValid else { return }
        if type == .screen {
            // The system also sends "nothing changed" notices; only complete
            // frames carry a picture.
            guard let attachments = CMSampleBufferGetSampleAttachmentsArray(sampleBuffer, createIfNecessary: false)
                    as? [[SCStreamFrameInfo: Any]],
                let raw = attachments.first?[.status] as? Int,
                SCFrameStatus(rawValue: raw) == .complete,
                let buffer = sampleBuffer.imageBuffer
            else { return }
            latest = buffer
            delivered += 1
            if !start.isValid { begin(at: sampleBuffer.presentationTimeStamp) }
        } else if let audioInput, start.isValid, audioInput.isReadyForMoreMediaData,
            sampleBuffer.presentationTimeStamp >= start
        {
            audioInput.append(sampleBuffer)
        }
    }

    // The screen only produces a frame when something changes, so writing
    // frames as they arrive gives a file with long gaps in it. A fixed clock
    // writes the newest picture every 1/fps instead: an even frame rate, with
    // unchanged pictures costing almost nothing to encode.
    private func begin(at time: CMTime) {
        start = time
        writer.startWriting()
        writer.startSession(atSourceTime: time)
        let timer = DispatchSource.makeTimerSource(queue: queue)
        timer.schedule(deadline: .now(), repeating: 1.0 / Double(options.fps), leeway: .milliseconds(2))
        timer.setEventHandler { [weak self] in self?.tick() }
        timer.resume()
        self.timer = timer
    }

    private func tick() {
        guard !stopping, let latest, videoInput.isReadyForMoreMediaData else { return }
        let elapsed = CMClockGetTime(CMClockGetHostTimeClock()) - start
        let index = Int64((elapsed.seconds * Double(options.fps)).rounded(.down))
        guard index > lastTick else { return }
        lastTick = index
        let time = start + CMTime(value: index, timescale: CMTimeScale(options.fps))
        if adaptor.append(latest, withPresentationTime: time) { written += 1 }
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        log("capture stopped: \(error.localizedDescription)")
        stop()
    }

    func stop() {
        queue.async {
            guard !self.stopping else { return }
            self.stopping = true
            self.timer?.cancel()
            Task {
                try? await self.stream?.stopCapture()
                guard self.start.isValid else { fail("no frames were captured") }
                self.videoInput.markAsFinished()
                self.audioInput?.markAsFinished()
                let end = self.start + CMTime(value: self.lastTick + 1, timescale: CMTimeScale(self.options.fps))
                self.writer.endSession(atSourceTime: end)
                await self.writer.finishWriting()
                if self.writer.status != .completed {
                    fail("could not finish the file: \(self.writer.error?.localizedDescription ?? "unknown")")
                }
                // How many pictures the system delivered against how many
                // were written says whether capture itself kept up.
                log("done: delivered=\(self.delivered) written=\(self.written) seconds=\(String(format: "%.1f", Double(self.lastTick + 1) / Double(self.options.fps)))")
                exit(0)
            }
        }
    }
}

let options = parseOptions()

// Without Screen Recording permission nothing can be captured. Asking here
// makes macOS show its prompt and list the app under Privacy & Security;
// the marker at the start of the message is what the app looks for.
if !CGPreflightScreenCaptureAccess() {
    CGRequestScreenCaptureAccess()
    fail("screen-recording-permission: macOS has not allowed this app to record the screen")
}

let recorder = Recorder(options: options)

// Ctrl+C and termination finish the file rather than cutting it off.
var signalSources: [DispatchSourceSignal] = []
for number in [SIGINT, SIGTERM] {
    signal(number, SIG_IGN)
    let source = DispatchSource.makeSignalSource(signal: number, queue: .main)
    source.setEventHandler { recorder.stop() }
    source.resume()
    signalSources.append(source)
}

// "q" on stdin stops too, which is how the app asks (same as ffmpeg).
Thread.detachNewThread {
    while let line = readLine(strippingNewline: true) {
        if line.contains("q") { recorder.stop(); return }
    }
}

Task {
    do {
        try await recorder.run()
    } catch {
        let reason = error.localizedDescription
        if reason.contains("TCC") { fail("screen-recording-permission: \(reason)") }
        fail("could not start capture: \(reason)")
    }
}
dispatchMain()
