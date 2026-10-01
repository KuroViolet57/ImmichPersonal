package com.kuroviolet.imagepanel.diag

import android.content.Context
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import android.net.Uri
import android.os.SystemClock
import androidx.annotation.OptIn
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.media3.common.C
import androidx.media3.common.Format
import androidx.media3.common.PlaybackException
import androidx.media3.common.Player
import androidx.media3.common.util.UnstableApi
import androidx.media3.datasource.DataSource
import androidx.media3.datasource.DataSpec
import androidx.media3.datasource.HttpDataSource
import androidx.media3.datasource.TransferListener
import androidx.media3.exoplayer.DecoderReuseEvaluation
import androidx.media3.exoplayer.ExoPlayer
import androidx.media3.exoplayer.analytics.AnalyticsListener
import androidx.media3.exoplayer.analytics.AnalyticsListener.EventTime
import androidx.media3.exoplayer.analytics.PlaybackStatsListener
import androidx.media3.exoplayer.source.LoadEventInfo
import androidx.media3.exoplayer.source.MediaLoadData
import com.kuroviolet.imagepanel.Graph
import com.kuroviolet.imagepanel.model.Asset
import java.io.IOException
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicLong
import kotlin.random.Random

/** Whether the video player shows its live stats; stays on for the next video until switched off. */
object VideoStats {
    var visible by mutableStateOf(false)
}

/**
 * Watches one video playback and writes what happened to the app log (tag "video"), so a stutter can be
 * put down to the network, the panel/Immich, or the phone:
 *  - every HTTP request the player makes: where in the file, and how long until the first byte came back;
 *  - how fast bytes really arrive while the player is reading (time spent blocked inside read()), next to
 *    the rate the file needs to play (its size × 8 ÷ its length);
 *  - every stall (buffering after playback had started, not caused by a seek) and how long it lasted;
 *  - the format and decoder, dropped frames, audio underruns, and load errors the player retried quietly;
 *  - a summary with a verdict when the video is closed (counts from Media3's PlaybackStatsListener).
 * [id] goes to the panel as X-Playback-Id, so the panel's journal lines for this playback can be matched.
 */
@OptIn(UnstableApi::class)
class PlaybackProbe(private val context: Context, private val asset: Asset) {
    val id: String = "%06x".format(Random.nextInt(0x1000000))
    private val tag = "video"
    private val created = SystemClock.elapsedRealtime()
    private var player: ExoPlayer? = null
    private val stats = PlaybackStatsListener(false, null)

    // Written on the loading thread, read on the main thread.
    private val bytes = AtomicLong()
    private val readNanos = AtomicLong()
    private val requests = AtomicInteger()
    @Volatile private var fileBytes = -1L
    @Volatile private var lastFirstByteMs = -1L

    // Main thread only.
    private var readyOnce = false
    private var seeking = false
    private var stallStartedAt = -1L
    private var stalls = 0
    private var stallMs = 0L
    private var firstFrameMs = -1L
    private var dropped = 0L
    private var underruns = 0
    private var loadErrors = 0
    private var decoder = ""
    private var videoFormat = ""
    private var lastLogAt = 0L
    private var window = Window(created, 0, 0)    // counters at the last progress line
    private var live = Window(0, 0, 0)            // counters at the last on-screen refresh
    private var liveMbps = 0.0

    private data class Window(val atMs: Long, val bytes: Long, val readNanos: Long)

    fun dataSourceFactory(upstream: DataSource.Factory) = DataSource.Factory { Metered(upstream.createDataSource()) }

    fun attach(player: ExoPlayer) {
        this.player = player
        player.addAnalyticsListener(stats)
        player.addAnalyticsListener(listener)
        Diag.i(tag, "▶ $id open “${asset.name}” (${asset.id}) · panel ${panelLine()} · ${networkLine(context)}")
    }

    /** Call once a second from the player screen: writes a progress line every 5 s while playing or stalled. */
    fun tick() {
        val p = player ?: return
        val now = SystemClock.elapsedRealtime()
        if (!p.playWhenReady || p.playbackState == Player.STATE_IDLE || p.playbackState == Player.STATE_ENDED) return
        if (now - lastLogAt < 5000) return
        val w = Window(now, bytes.get(), readNanos.get())
        if (lastLogAt > 0) {
            val got = w.bytes - window.bytes
            val state = if (p.playbackState == Player.STATE_BUFFERING) "STALLED" else "playing"
            val loading = if (p.isLoading) "loading" else "buffer full, not loading"
            Diag.i(tag, "$id ${fmtTime(p.currentPosition)}/${fmtTime(p.duration)} $state · buffer +${secs(p.totalBufferedDuration)} ($loading) · " +
                "last ${secs(w.atMs - window.atMs)}: ${mb(got)} arrived = ${mbps(got, w.atMs - window.atMs)} · file needs ${neededText()}")
        }
        window = w
        lastLogAt = now
    }

    /** What the on-screen stats show; call every half second or so. */
    fun liveLines(): List<String> {
        val p = player ?: return emptyList()
        val now = SystemClock.elapsedRealtime()
        val w = Window(now, bytes.get(), readNanos.get())
        if (now - live.atMs >= 1500) {
            liveMbps = if (live.atMs == 0L) 0.0 else (w.bytes - live.bytes) * 8.0 / (now - live.atMs) / 1000.0
            live = w
        }
        val state = when (p.playbackState) {
            Player.STATE_BUFFERING -> "buffering"
            Player.STATE_READY -> if (p.isPlaying) "playing" else "paused"
            Player.STATE_ENDED -> "ended"
            else -> "idle"
        }
        return listOf(
            "arriving ${"%.1f".format(liveMbps)} Mbit/s · while reading ${readMbpsText()} · file needs ${neededText()}",
            "buffer +${secs(p.totalBufferedDuration)} · ${if (p.isLoading) "loading" else "not loading"} · $state",
            "stalls $stalls (${secs(stallMs + currentStallMs())}) · requests ${requests.get()} · first byte ${if (lastFirstByteMs >= 0) "$lastFirstByteMs ms" else "–"} · dropped $dropped",
            listOf(videoFormat, decoder).filter { it.isNotEmpty() }.joinToString(" · "),
            "${mb(bytes.get())} of ${if (fileBytes > 0) mb(fileBytes) else "?"} · ${panelLine()} · id $id",
        ).filter { it.isNotBlank() }
    }

    /** Writes the summary. Call before the player is released. */
    fun finish() {
        val p = player ?: return
        val s = stats.playbackStats
        val open = SystemClock.elapsedRealtime() - created
        val rebuffers = s?.totalRebufferCount ?: stalls
        val waited = s?.totalRebufferTimeMs ?: (stallMs + currentStallMs())
        Diag.i(tag, buildString {
            append("■ $id summary “${asset.name}”: open ${secs(open)}, at ${fmtTime(p.currentPosition)} of ${fmtTime(p.duration)}")
            append(" · startup ${if (firstFrameMs >= 0) secs(firstFrameMs - created) else "never showed a frame"}")
            append(" · $rebuffers stalls, ${secs(waited)} waiting")
            s?.maxRebufferTimeMs?.takeIf { it > 0 }?.let { append(" (longest ${secs(it)})") }
            s?.totalSeekCount?.takeIf { it > 0 }?.let { append(" · $it seeks") }
            append(" · ${mb(bytes.get())} in ${requests.get()} requests, ${readMbpsText()} while reading · file needs ${neededText()}")
            append(" · dropped $dropped frames")
            if (underruns > 0) append(" · $underruns audio underruns")
            if (loadErrors > 0) append(" · $loadErrors load errors retried")
            if (decoder.isNotEmpty()) append(" · $decoder")
        })
        Diag.i(tag, "■ $id verdict: ${verdict(rebuffers)}")
        p.removeAnalyticsListener(listener)
        p.removeAnalyticsListener(stats)
        player = null
    }

    private fun verdict(rebuffers: Int): String {
        val read = readMbps()
        val need = neededMbps()
        val p = player
        val frames = p?.videoFormat?.frameRate?.takeIf { it > 0 }?.let { it.toDouble() * p.currentPosition / 1000.0 } ?: 0.0
        return when {
            rebuffers == 0 && firstFrameMs >= 0 -> "played without stalling"
            need > 0 && read > 0 && read < need * 1.15 ->
                "the connection is too slow for this file: data arrived at ${"%.1f".format(read)} Mbit/s, it needs ${"%.1f".format(need)}"
            loadErrors > 0 -> "requests to the panel failed and were retried ($loadErrors times) — check the panel log for id $id"
            frames > 0 && dropped > frames * 0.05 -> "the phone dropped $dropped frames — decoding couldn't keep up"
            requests.get() > 3 + (stats.playbackStats?.totalSeekCount ?: 0) * 2 ->
                "the player had to re-request the file ${requests.get()} times — check the request lines above"
            else -> "stalled although data came fast enough (${"%.1f".format(read)} vs ${"%.1f".format(need)} Mbit/s needed) — see the panel log for id $id"
        }
    }

    // ---------------------------------------------------------------- numbers

    private fun readMbps(): Double = readNanos.get().takeIf { it > 0 }?.let { bytes.get() * 8.0 / it * 1000.0 } ?: 0.0
    private fun readMbpsText(): String = if (readNanos.get() > 0) "%.1f Mbit/s".format(readMbps()) else "–"

    /** The average rate the file needs to play in real time: its size over its length. */
    private fun neededMbps(): Double {
        val duration = player?.duration ?: C.TIME_UNSET
        if (fileBytes <= 0 || duration == C.TIME_UNSET || duration <= 0) return 0.0
        return fileBytes * 8.0 / duration / 1000.0
    }
    private fun neededText(): String = neededMbps().takeIf { it > 0 }?.let { "%.1f Mbit/s".format(it) } ?: "?"

    private fun currentStallMs(): Long = if (stallStartedAt >= 0) SystemClock.elapsedRealtime() - stallStartedAt else 0

    private fun panelLine(): String {
        val host = Uri.parse(Graph.settings.baseUrl).host ?: "?"
        val parts = host.split('.').mapNotNull { it.toIntOrNull() }
        val tailscale = parts.size == 4 && parts[0] == 100 && parts[1] in 64..127
        return if (tailscale) "$host (Tailscale)" else host
    }

    // ---------------------------------------------------------------- player events

    private val listener = object : AnalyticsListener {
        override fun onPlaybackStateChanged(eventTime: EventTime, state: Int) {
            val now = eventTime.realtimeMs
            when (state) {
                Player.STATE_BUFFERING -> if (readyOnce && !seeking) {
                    stalls++
                    stallStartedAt = now
                    val p = player
                    Diag.w(tag, "$id stall #$stalls at ${fmtTime(p?.currentPosition ?: 0)} · buffer +${secs(p?.totalBufferedDuration ?: 0)} · " +
                        "arriving ${mbps(bytes.get() - window.bytes, now - window.atMs)} lately, file needs ${neededText()}")
                }
                Player.STATE_READY -> {
                    if (!readyOnce) Diag.i(tag, "$id ready after ${now - created} ms")
                    if (stallStartedAt >= 0) {
                        val d = now - stallStartedAt
                        stallMs += d
                        Diag.i(tag, "$id resumed after ${secs(d)}")
                        stallStartedAt = -1
                    }
                    readyOnce = true
                    seeking = false
                }
                Player.STATE_ENDED -> Diag.i(tag, "$id reached the end")
            }
        }

        override fun onPositionDiscontinuity(eventTime: EventTime, oldPosition: Player.PositionInfo, newPosition: Player.PositionInfo, reason: Int) {
            if (reason != Player.DISCONTINUITY_REASON_SEEK) return
            seeking = true
            if (stallStartedAt >= 0) {         // a seek ends a stall in progress
                stallMs += eventTime.realtimeMs - stallStartedAt
                stallStartedAt = -1
            }
            Diag.i(tag, "$id seek ${fmtTime(oldPosition.positionMs)} → ${fmtTime(newPosition.positionMs)}")
        }

        override fun onRenderedFirstFrame(eventTime: EventTime, output: Any, renderTimeMs: Long) {
            if (firstFrameMs >= 0) return
            firstFrameMs = eventTime.realtimeMs
            Diag.i(tag, "$id first frame after ${firstFrameMs - created} ms")
        }

        override fun onVideoInputFormatChanged(eventTime: EventTime, format: Format, decoderReuseEvaluation: DecoderReuseEvaluation?) {
            videoFormat = buildString {
                append("${format.width}×${format.height}")
                if (format.frameRate > 0) append(" ${"%.0f".format(format.frameRate)} fps")
                append(" ${format.codecs ?: format.sampleMimeType ?: "?"}")
            }
            val rate = format.averageBitrate.takeIf { it > 0 } ?: format.peakBitrate.takeIf { it > 0 }
            Diag.i(tag, "$id video $videoFormat${rate?.let { " · stream ${"%.1f".format(it / 1e6)} Mbit/s" } ?: ""} · ${format.containerMimeType ?: ""}")
        }

        override fun onAudioInputFormatChanged(eventTime: EventTime, format: Format, decoderReuseEvaluation: DecoderReuseEvaluation?) {
            Diag.i(tag, "$id audio ${format.codecs ?: format.sampleMimeType} ${format.sampleRate} Hz × ${format.channelCount}")
        }

        override fun onVideoDecoderInitialized(eventTime: EventTime, decoderName: String, initializedTimestampMs: Long, initializationDurationMs: Long) {
            decoder = decoderName
            Diag.i(tag, "$id decoder $decoderName (set up in $initializationDurationMs ms)")
        }

        override fun onDroppedVideoFrames(eventTime: EventTime, droppedFrames: Int, elapsedMs: Long) {
            dropped += droppedFrames
            Diag.w(tag, "$id dropped $droppedFrames frames over ${secs(elapsedMs)}")
        }

        override fun onAudioUnderrun(eventTime: EventTime, bufferSize: Int, bufferSizeMs: Long, elapsedSinceLastFeedMs: Long) {
            underruns++
            if (underruns <= 5) Diag.w(tag, "$id audio underrun (nothing fed for $elapsedSinceLastFeedMs ms)")
        }

        override fun onLoadError(eventTime: EventTime, loadEventInfo: LoadEventInfo, mediaLoadData: MediaLoadData, error: IOException, wasCanceled: Boolean) {
            loadErrors++
            Diag.w(tag, "$id load error after ${mb(loadEventInfo.bytesLoaded)} — the player retries: ${describe(error)}")
        }

        override fun onPlayerError(eventTime: EventTime, error: PlaybackException) {
            Diag.e(tag, "$id player error ${error.errorCodeName}: ${describe(error)}")
        }

        override fun onVideoCodecError(eventTime: EventTime, videoCodecError: Exception) {
            Diag.w(tag, "$id video codec error: ${describe(videoCodecError)}")
        }
    }

    // ---------------------------------------------------------------- the network side

    /** Wraps the player's HTTP source to time each request and every read. */
    private inner class Metered(private val inner: DataSource) : DataSource {
        private var n = 0
        private var position = 0L
        private var got = 0L

        override fun addTransferListener(transferListener: TransferListener) = inner.addTransferListener(transferListener)

        override fun open(dataSpec: DataSpec): Long {
            n = requests.incrementAndGet()
            position = dataSpec.position
            got = 0
            val started = SystemClock.elapsedRealtime()
            val length = try {
                inner.open(dataSpec)
            } catch (e: IOException) {
                Diag.w(tag, "$id request #$n from ${mb(position)} failed after ${SystemClock.elapsedRealtime() - started} ms: ${describe(e)}")
                throw e
            }
            val ms = SystemClock.elapsedRealtime() - started
            lastFirstByteMs = ms
            val total = inner.responseHeaders.entries.firstOrNull { it.key.equals("Content-Range", true) }
                ?.value?.firstOrNull()?.substringAfter('/')?.toLongOrNull()
                ?: if (dataSpec.position == 0L && length != C.LENGTH_UNSET.toLong()) length else null
            if (total != null && total > 0) fileBytes = total
            val code = (inner as? HttpDataSource)?.responseCode ?: 0
            if (n <= 40 || n % 20 == 0) {
                Diag.i(tag, "$id request #$n from ${mb(position)}${if (fileBytes > 0) " of ${mb(fileBytes)}" else ""} → $code, first byte after $ms ms")
            }
            return length
        }

        override fun read(buffer: ByteArray, offset: Int, length: Int): Int {
            val started = System.nanoTime()
            val r = try {
                inner.read(buffer, offset, length)
            } catch (e: IOException) {
                Diag.w(tag, "$id request #$n broke after ${mb(got)} (at ${mb(position + got)}): ${describe(e)}")
                throw e
            }
            readNanos.addAndGet(System.nanoTime() - started)
            if (r > 0) {
                bytes.addAndGet(r.toLong())
                got += r
            }
            return r
        }

        override fun getUri(): Uri? = inner.uri
        override fun getResponseHeaders(): Map<String, List<String>> = inner.responseHeaders
        override fun close() = inner.close()
    }

    companion object {
        fun fmtTime(ms: Long): String {
            if (ms <= 0 || ms == C.TIME_UNSET) return "0:00"
            val t = ms / 1000
            return if (t >= 3600) "%d:%02d:%02d".format(t / 3600, t % 3600 / 60, t % 60) else "%d:%02d".format(t / 60, t % 60)
        }
        fun secs(ms: Long): String = "%.1f s".format(ms / 1000.0)
        fun mb(bytes: Long): String = "%.1f MB".format(bytes / 1e6)
        fun mbps(bytes: Long, ms: Long): String = if (ms > 0) "%.1f Mbit/s".format(bytes * 8.0 / ms / 1000.0) else "–"

        /** An exception with its causes, one after another (the useful part is often two levels down). */
        fun describe(e: Throwable): String = generateSequence(e) { it.cause }.take(4)
            .joinToString(" ← ") { "${it.javaClass.simpleName}: ${it.message}" }

        /** How the phone is connected right now, e.g. "network VPN+Wi-Fi, Android estimates ↓48 Mbit/s". */
        fun networkLine(context: Context): String {
            val cm = context.getSystemService(ConnectivityManager::class.java) ?: return "network ?"
            val caps = cm.getNetworkCapabilities(cm.activeNetwork) ?: return "no active network"
            val kinds = listOf(
                NetworkCapabilities.TRANSPORT_VPN to "VPN", NetworkCapabilities.TRANSPORT_WIFI to "Wi-Fi",
                NetworkCapabilities.TRANSPORT_CELLULAR to "mobile", NetworkCapabilities.TRANSPORT_ETHERNET to "Ethernet",
            ).filter { caps.hasTransport(it.first) }.joinToString("+") { it.second }.ifEmpty { "other" }
            return "network $kinds, Android estimates ↓${caps.linkDownstreamBandwidthKbps / 1000} ↑${caps.linkUpstreamBandwidthKbps / 1000} Mbit/s"
        }
    }
}
