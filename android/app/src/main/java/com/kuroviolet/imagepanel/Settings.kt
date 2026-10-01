package com.kuroviolet.imagepanel

import android.content.Context
import android.net.Uri
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue

/** Where the panel is and how to get in. Observable, so the UI follows changes. */
class Settings(context: Context) {
    private val prefs = context.getSharedPreferences("settings", Context.MODE_PRIVATE)

    var baseUrl by mutableStateOf(prefs.getString("baseUrl", "") ?: "")
        private set
    var token by mutableStateOf(prefs.getString("token", "") ?: "")
        private set
    /** Smallest thumbnail width in dp; the grid fits as many columns as it can. */
    var tileDp by mutableIntStateOf(prefs.getInt("tileDp", 104))
        private set

    val configured: Boolean get() = baseUrl.isNotEmpty() && token.isNotEmpty()

    fun save(baseUrl: String, token: String) {
        this.baseUrl = baseUrl.trimEnd('/')
        this.token = token
        prefs.edit().putString("baseUrl", this.baseUrl).putString("token", token).apply()
    }

    fun forget() = save("", "")

    fun setTile(dp: Int) {
        tileDp = dp
        prefs.edit().putInt("tileDp", dp).apply()
    }

    val maskedToken: String
        get() = if (token.length <= 6) "•".repeat(token.length) else token.take(3) + "…" + token.takeLast(3)

    companion object {
        /**
         * Reads the panel link as the desktop shows it: "http://host:8777/?t=KEY" (also with a path or
         * extra parameters). Returns (base url, key) or null when it doesn't look like a link.
         */
        fun parseLink(text: String): Pair<String, String>? {
            val raw = text.trim().split(Regex("\\s+")).firstOrNull { it.startsWith("http://") || it.startsWith("https://") }
                ?: return null
            val uri = runCatching { Uri.parse(raw) }.getOrNull() ?: return null
            val scheme = uri.scheme ?: return null
            val host = uri.host ?: return null
            val port = if (uri.port > 0) ":${uri.port}" else ""
            val key = uri.getQueryParameter("t") ?: ""
            return "$scheme://$host$port" to key
        }

        /** "100.64.1.2:8777" or "desktop:8777" becomes "http://…". */
        fun normalizeAddress(text: String): String {
            val t = text.trim().trimEnd('/')
            if (t.isEmpty()) return t
            return if (t.startsWith("http://") || t.startsWith("https://")) t else "http://$t"
        }
    }
}
