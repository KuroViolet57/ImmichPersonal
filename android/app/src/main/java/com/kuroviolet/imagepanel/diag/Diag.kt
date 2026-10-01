package com.kuroviolet.imagepanel.diag

import android.content.Context
import android.os.Build
import androidx.compose.runtime.mutableIntStateOf
import com.kuroviolet.imagepanel.BuildConfig
import okhttp3.Interceptor
import okhttp3.Response
import java.io.File
import java.io.PrintWriter
import java.io.StringWriter
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * The app's own log: kept in memory for the Diagnostics screen and written to a file, so it
 * survives restarts and crashes. "Share logs" sends it as a text file.
 */
object Diag {
    data class Entry(val time: Long, val level: Char, val tag: String, val message: String) {
        fun line(): String = "${STAMP.get()!!.format(Date(time))} $level/$tag: $message"
    }

    private const val MAX_ENTRIES = 3000
    private const val MAX_FILE = 2L * 1024 * 1024
    private val STAMP = object : ThreadLocal<SimpleDateFormat>() {
        override fun initialValue() = SimpleDateFormat("MM-dd HH:mm:ss.SSS", Locale.US)
    }

    private val entries = ArrayDeque<Entry>()
    private var logFile: File? = null
    private var crashFile: File? = null

    /** Bumped on every new entry, so a screen showing the log can follow it. */
    val revision = mutableIntStateOf(0)
    var requests = 0
        private set
    var failures = 0
        private set

    fun init(context: Context) {
        val dir = File(context.filesDir, "logs").apply { mkdirs() }
        logFile = File(dir, "app.log").also { f ->
            if (f.exists() && f.length() > MAX_FILE) f.renameTo(File(dir, "app.old.log"))
        }
        crashFile = File(dir, "last-crash.txt")
        // Keep the tail of the previous session visible after a restart.
        runCatching {
            logFile?.takeIf { it.exists() }?.readLines()?.takeLast(300)?.forEach { line ->
                synchronized(entries) { entries.addLast(Entry(0, '·', "prev", line)) }
            }
        }
        val previous = Thread.getDefaultUncaughtExceptionHandler()
        Thread.setDefaultUncaughtExceptionHandler { thread, error ->
            runCatching {
                e("crash", "uncaught on ${thread.name}", error)
                crashFile?.writeText(buildString {
                    appendLine("Image Panel ${BuildConfig.VERSION_NAME} crashed at ${Date()}")
                    appendLine(deviceLine())
                    appendLine()
                    appendLine(stack(error))
                    appendLine()
                    appendLine("Last log lines:")
                    snapshot().takeLast(150).forEach { appendLine(it.line()) }
                })
            }
            previous?.uncaughtException(thread, error)
        }
    }

    fun i(tag: String, message: String) = add('I', tag, message)
    fun w(tag: String, message: String, error: Throwable? = null) =
        add('W', tag, if (error == null) message else "$message: ${error.javaClass.simpleName}: ${error.message}")
    fun e(tag: String, message: String, error: Throwable? = null) =
        add('E', tag, if (error == null) message else "$message\n${stack(error)}")

    fun network(ok: Boolean) {
        requests++
        if (!ok) failures++
    }

    private fun add(level: Char, tag: String, message: String) {
        val entry = Entry(System.currentTimeMillis(), level, tag, message)
        synchronized(entries) {
            entries.addLast(entry)
            while (entries.size > MAX_ENTRIES) entries.removeFirst()
            runCatching { logFile?.appendText(entry.line() + "\n") }
        }
        android.util.Log.println(
            when (level) { 'E' -> android.util.Log.ERROR; 'W' -> android.util.Log.WARN; else -> android.util.Log.INFO },
            "ImagePanel/$tag", message,
        )
        revision.intValue++
    }

    fun snapshot(): List<Entry> = synchronized(entries) { entries.toList() }

    fun clear() {
        synchronized(entries) {
            entries.clear()
            runCatching { logFile?.writeText("") }
        }
        revision.intValue++
    }

    fun lastCrash(): String? = crashFile?.takeIf { it.exists() }?.readText()
    fun clearCrash() { crashFile?.delete(); revision.intValue++ }

    fun deviceLine(): String =
        "Device: ${Build.MANUFACTURER} ${Build.MODEL} (${Build.DEVICE}), Android ${Build.VERSION.RELEASE} (API ${Build.VERSION.SDK_INT})"

    fun stack(error: Throwable): String = StringWriter().also { error.printStackTrace(PrintWriter(it)) }.toString()

    /** Everything useful for a bug report, as one text. */
    fun report(header: String): String = buildString {
        appendLine("Image Panel ${BuildConfig.VERSION_NAME} (${BuildConfig.VERSION_CODE}), built ${Date(BuildConfig.BUILD_TIME)}")
        appendLine(deviceLine())
        appendLine(header)
        appendLine("Requests this session: $requests, failed: $failures")
        lastCrash()?.let { appendLine(); appendLine("=== Last crash ==="); appendLine(it) }
        appendLine()
        appendLine("=== Log ===")
        snapshot().forEach { appendLine(it.line()) }
    }
}

/** Logs every request: method, path (never the key), status, time and size. */
class DiagInterceptor : Interceptor {
    override fun intercept(chain: Interceptor.Chain): Response {
        val request = chain.request()
        val url = request.url
        val path = url.encodedPath + (url.query?.let { q -> "?" + q.replace(Regex("(^|&)t=[^&]*"), "$1t=…") } ?: "")
        val started = System.nanoTime()
        return try {
            val response = chain.proceed(request)
            val ms = (System.nanoTime() - started) / 1_000_000
            val ok = response.isSuccessful || response.code == 206 || response.code == 304
            Diag.network(ok)
            val size = (response.body?.contentLength() ?: -1L).takeIf { it >= 0 }?.let { " ${it / 1024}KB" } ?: ""
            val quiet = ok && (url.encodedPath.startsWith("/thumb/") || url.encodedPath.startsWith("/media/"))
            if (!quiet || ms > 4000) {
                val msg = "${request.method} $path → ${response.code} in ${ms}ms$size"
                if (ok) Diag.i("net", msg) else Diag.w("net", msg)
            }
            response
        } catch (error: Exception) {
            val ms = (System.nanoTime() - started) / 1_000_000
            Diag.network(false)
            Diag.w("net", "${request.method} $path failed after ${ms}ms", error)
            throw error
        }
    }
}
