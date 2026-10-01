package com.kuroviolet.imagepanel.model

import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import com.kuroviolet.imagepanel.Graph
import com.kuroviolet.imagepanel.net.a
import com.kuroviolet.imagepanel.net.b
import com.kuroviolet.imagepanel.net.i
import com.kuroviolet.imagepanel.net.o
import com.kuroviolet.imagepanel.net.obj
import com.kuroviolet.imagepanel.net.str
import com.kuroviolet.imagepanel.net.strings
import com.kuroviolet.imagepanel.ui.components.FileRequest
import kotlinx.serialization.json.add
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import kotlinx.serialization.json.putJsonArray

/** Requests one screen leaves for another (e.g. the viewer's "Similar" for Search+). */
object Pending {
    var searchPlusLike by mutableStateOf<String?>(null)
    var searchLike by mutableStateOf<String?>(null)
}

object Ops {
    /** The panel's "Add / Move to album" (POST /api/file). Returns a one-line summary. */
    suspend fun file(ids: List<String>, req: FileRequest): String {
        val data = Graph.api.post("/api/file", buildJsonObject {
            putJsonArray("assetIds") { ids.forEach { add(it) } }
            put("album", req.album)
            put("move", req.move)
            put("createAlbum", true)
            put("archive", req.archive)
            put("favorite", req.favorite)
        }).obj()
        val added = (data.i("added") ?: 0) + if (req.move) (data.i("duplicates") ?: 0) else 0
        val bits = mutableListOf("${if (req.move) "Moved" else "Added"} $added to “${data.str("album", req.album)}”")
        if (data.b("created") == true) bits += "(new album)"
        if (!req.move && (data.i("duplicates") ?: 0) > 0) bits += "· ${data.i("duplicates")} were already there"
        if (req.move && (data.i("removedTotal") ?: 0) > 0) bits += "· taken out of ${data.o("removedFrom").size} other album(s)"
        val failures = data.a("failures").strings()
        if (failures.isNotEmpty()) bits += "· ${failures.size} problem(s): ${failures.first()}"
        RefData.loadAlbums()
        return bits.joinToString(" ")
    }
}
