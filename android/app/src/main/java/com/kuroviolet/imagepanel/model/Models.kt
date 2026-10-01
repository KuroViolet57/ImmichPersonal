package com.kuroviolet.imagepanel.model

import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import com.kuroviolet.imagepanel.Graph
import com.kuroviolet.imagepanel.diag.Diag
import com.kuroviolet.imagepanel.net.ApiException
import com.kuroviolet.imagepanel.net.a
import com.kuroviolet.imagepanel.net.arr
import com.kuroviolet.imagepanel.net.d
import com.kuroviolet.imagepanel.net.i
import com.kuroviolet.imagepanel.net.obj
import com.kuroviolet.imagepanel.net.objects
import com.kuroviolet.imagepanel.net.s
import com.kuroviolet.imagepanel.net.str
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.serialization.json.putJsonObject
import kotlinx.serialization.json.put
import kotlinx.serialization.json.buildJsonObject
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject

/** One photo or video, as the panel's lists describe it. */
data class Asset(
    val id: String,
    val name: String = "",
    val type: String = "IMAGE",
    val date: String = "",
    val score: Double? = null,
    val added: String = "",
) {
    val isVideo: Boolean get() = type == "VIDEO"
    val isAnimated: Boolean get() = name.lowercase().let { it.endsWith(".gif") || it.endsWith(".webp") || it.endsWith(".apng") }

    companion object {
        fun from(o: JsonObject): Asset? {
            val id = o.s("id") ?: return null
            return Asset(
                id = id,
                name = o.str("name"),
                type = o.str("type", "IMAGE").ifEmpty { "IMAGE" },
                date = (o.s("date") ?: o.s("taken") ?: "").take(10),
                score = o.d("score"),
                added = o.str("added"),
            )
        }

        fun list(a: JsonArray): List<Asset> = a.objects().mapNotNull { from(it) }
    }
}

data class Album(
    val id: String,
    val name: String,
    val count: Int,
    val thumb: String? = null,
    val description: String = "",
    val shared: Boolean = false,
    val updatedAt: String = "",
)

data class Person(val id: String, val name: String, val label: String)

/** Album names and people: suggestions used across the app, loaded once and refreshed after changes. */
object RefData {
    var albums by mutableStateOf<List<Album>>(emptyList())
        private set
    var people by mutableStateOf<List<Person>>(emptyList())
        private set
    var unnamedPeople by mutableStateOf(0)
        private set

    suspend fun loadAlbums() {
        try {
            albums = Graph.api.get("/api/albums").arr().objects().map {
                Album(it.str("id"), it.str("name"), it.i("count") ?: 0)
            }
        } catch (e: ApiException) {
            Diag.w("ref", "albums: ${e.message}")
        }
    }

    suspend fun loadPeople() {
        try {
            val data = Graph.api.get("/api/people").obj()
            val counts = HashMap<String, Int>()
            people = data.a("people").objects().map {
                val name = it.str("name")
                val n = (counts[name] ?: 0) + 1
                counts[name] = n
                Person(it.str("id"), name, if (n > 1) "$name ($n)" else name)
            }
            unnamedPeople = data.i("unnamed") ?: 0
        } catch (e: ApiException) {
            Diag.w("ref", "people: ${e.message}")
        }
    }

    fun personName(id: String): String = people.firstOrNull { it.id == id }?.name ?: "someone"
}

/** Sort choices remembered on the panel, shared with the web panel (so a sort picked anywhere sticks). */
object PanelPrefs {
    var albumsSort by mutableStateOf("name")
    var albumSort by mutableStateOf("taken_desc")
    var searchEngine by mutableStateOf("immich")

    suspend fun load() {
        try {
            val d = Graph.api.get("/api/prefs").obj()
            d.s("albumsSort")?.let { albumsSort = it }
            d.s("albumSort")?.let { albumSort = it }
            d.s("searchEngine")?.let { searchEngine = it }
        } catch (e: ApiException) {
            Diag.w("prefs", "load: ${e.message}")
        }
    }

    suspend fun save(key: String, value: String) {
        try {
            Graph.api.post("/api/prefs", buildJsonObject { putJsonObject("changes") { put(key, value) } })
        } catch (e: Exception) {
            Diag.w("prefs", "save $key: ${e.message}")
        }
    }
}

/** Short messages for the snackbar, from anywhere. */
object UiBus {
    private val _messages = MutableSharedFlow<Pair<String, Boolean>>(extraBufferCapacity = 16)
    val messages: SharedFlow<Pair<String, Boolean>> = _messages

    fun toast(message: String) {
        _messages.tryEmit(message to false)
    }

    fun error(message: String) {
        _messages.tryEmit(message to true)
    }

    fun error(e: Throwable, what: String = "") {
        val msg = when (e) {
            is ApiException -> e.message ?: "Error"
            else -> "${e.javaClass.simpleName}: ${e.message}"
        }
        if (e !is ApiException) Diag.e("ui", "error $what", e) else Diag.w("ui", "$what: $msg")
        error(msg)
    }
}

/** What the viewer shows, and how it talks back to the grid it was opened from (selection). */
interface ViewerHooks {
    fun isSelected(id: String): Boolean
    fun toggle(id: String)
}

object ViewerSession {
    var items: List<Asset> = emptyList()
    var start: Int = 0
    var hooks: ViewerHooks? = null
    var title: String = ""

    fun open(items: List<Asset>, index: Int, hooks: ViewerHooks?, title: String = "") {
        this.items = items
        this.start = index.coerceIn(0, (items.size - 1).coerceAtLeast(0))
        this.hooks = hooks
        this.title = title
    }
}
