package com.kuroviolet.imagepanel.ui.more

import android.content.Intent
import android.net.Uri
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Add
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material3.Card
import androidx.compose.material3.FilledTonalButton
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import androidx.lifecycle.viewmodel.compose.viewModel
import androidx.navigation.NavController
import com.kuroviolet.imagepanel.Graph
import com.kuroviolet.imagepanel.model.RefData
import com.kuroviolet.imagepanel.model.UiBus
import com.kuroviolet.imagepanel.net.a
import com.kuroviolet.imagepanel.net.b
import com.kuroviolet.imagepanel.net.d
import com.kuroviolet.imagepanel.net.i
import com.kuroviolet.imagepanel.net.o
import com.kuroviolet.imagepanel.net.obj
import com.kuroviolet.imagepanel.net.objects
import com.kuroviolet.imagepanel.net.s
import com.kuroviolet.imagepanel.net.str
import com.kuroviolet.imagepanel.net.strings
import com.kuroviolet.imagepanel.ui.albums.AlbumStore
import com.kuroviolet.imagepanel.ui.components.BusyLayer
import com.kuroviolet.imagepanel.ui.components.CheckRow
import com.kuroviolet.imagepanel.ui.components.Hint
import com.kuroviolet.imagepanel.ui.components.ScreenTop
import kotlinx.coroutines.launch
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.add
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import kotlinx.serialization.json.putJsonArray
import java.time.OffsetDateTime
import java.time.format.DateTimeFormatter
import java.time.format.FormatStyle

class SmartVm : ViewModel() {
    var themes by mutableStateOf<List<JsonObject>>(emptyList())
    var schedule by mutableStateOf<JsonObject?>(null)
    var busy by mutableStateOf<String?>(null)

    fun load(quiet: Boolean = false) {
        if (!quiet) busy = "Loading smart albums…"
        viewModelScope.launch {
            try {
                val data = Graph.api.get("/api/themes").obj()
                themes = data.a("themes").objects()
                schedule = data.o("schedule")
            } catch (e: Exception) { UiBus.error(e, "smart albums") } finally { busy = null }
        }
    }

    fun run(ids: List<String>?) {
        busy = if (ids == null) "Running all hourly smart albums…" else "Running smart album…"
        viewModelScope.launch {
            try {
                val data = Graph.api.post("/api/themes/run", buildJsonObject {
                    if (ids != null) putJsonArray("ids") { ids.forEach { add(it) } }
                }).obj()
                val results = data.a("results").objects()
                val errors = results.filter { it.s("error") != null }
                val added = results.sumOf { it.i("added") ?: 0 }
                val msg = "Added $added photo(s) across ${results.size} smart album(s)." +
                    if (errors.isNotEmpty()) " ${errors.size} failed: ${errors.first().str("error")}" else ""
                if (errors.isEmpty()) UiBus.toast(msg) else UiBus.error(msg)
                RefData.loadAlbums()
                runCatching { AlbumStore.load() }
                load(quiet = true)
            } catch (e: Exception) { UiBus.error(e, "run smart album"); busy = null }
        }
    }

    fun setEnabled(theme: JsonObject, on: Boolean) {
        viewModelScope.launch {
            try {
                val updated = JsonObject(theme + ("enabled" to JsonPrimitive(on)))
                Graph.api.post("/api/themes/save", buildJsonObject { put("theme", updated) })
                load(quiet = true)
            } catch (e: Exception) { UiBus.error(e, "smart album") }
        }
    }

    fun delete(theme: JsonObject, deleteAlbum: Boolean) {
        busy = "Deleting…"
        viewModelScope.launch {
            try {
                val data = Graph.api.post("/api/themes/delete", buildJsonObject {
                    put("id", theme.str("id")); put("deleteAlbum", deleteAlbum)
                }).obj()
                UiBus.toast("Deleted “${data.str("deleted")}”" + if (data.b("albumDeleted") == true) " and its album." else ". Its album is kept.")
                RefData.loadAlbums()
                load(quiet = true)
            } catch (e: Exception) { UiBus.error(e, "delete smart album"); busy = null }
        }
    }
}

/** One line describing what a smart album collects (same wording as the panel). */
fun themeSummary(t: JsonObject): String {
    val what = when (t.s("source")) {
        "like" -> "like photo ${t.str("like").take(8)}…"
        "none" -> "people only"
        else -> "“${t.str("description")}”"
    }
    val how = if (t.s("mode") == "top") "best %,d".format(t.i("limit") ?: 0) else "≥ %.3f".format(t.d("cutoff") ?: 0.0)
    val bits = mutableListOf(what, how)
    if (t.s("engine") == "searchplus") bits += "Search+ model"
    when (t.s("media")) { "VIDEO" -> bits += "only videos"; "IMAGE" -> bits += "only photos" }
    val people = t.a("people").strings()
    if (people.isNotEmpty()) bits += "with " + people.joinToString(if (t.s("people_match") == "any") " or " else " & ") { RefData.personName(it) }
    if (t.s("taken_after") != null || t.s("taken_before") != null) bits += "taken ${t.s("taken_after") ?: "…"} – ${t.s("taken_before") ?: "…"}"
    t.a("exclude_albums").strings().takeIf { it.isNotEmpty() }?.let { bits += "skip ${it.joinToString()}" }
    if (t.b("only_unfiled") == true) bits += "only unfiled"
    t.a("all_of").strings().takeIf { it.isNotEmpty() }?.let { bits += "+ ${it.joinToString()}" }
    t.a("none_of").strings().takeIf { it.isNotEmpty() }?.let { bits += "− ${it.joinToString()}" }
    if (t.b("archive") == true) bits += "archives"
    if (t.b("favorite") == true) bits += "favourites"
    return bits.joinToString(" · ")
}

@Composable
fun SmartAlbumsScreen(nav: NavController) {
    val vm: SmartVm = viewModel()
    val context = LocalContext.current
    LaunchedEffect(Unit) { vm.load() }
    var deleting by remember { mutableStateOf<JsonObject?>(null) }
    Scaffold(topBar = {
        ScreenTop("Smart albums", subtitle = vm.schedule?.let { s ->
            if (s.b("active") == true) "Hourly run on · next ${s.str("next").ifEmpty { "soon" }}" else "Hourly run is off"
        }) {
            IconButton(onClick = { nav.navigate("theme/new") }) { Icon(Icons.Filled.Add, "New smart album") }
            IconButton(onClick = { vm.load() }) { Icon(Icons.Filled.Refresh, "Refresh") }
        }
    }) { padding ->
        Box(Modifier.fillMaxSize().padding(padding)) {
            LazyColumn(contentPadding = PaddingValues(12.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                item {
                    Hint("A smart album keeps an album filled with the photos that match it; the hourly run adds new matches and never removes " +
                        "anything. Tap one to edit it, or + to make a new one.")
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp), modifier = Modifier.padding(top = 6.dp)) {
                        FilledTonalButton(onClick = { vm.run(null) }) { Text("Run all hourly ones") }
                        TextButton(onClick = { nav.navigate("theme/new") }) { Text("New smart album") }
                    }
                }
                if (vm.themes.isEmpty() && vm.busy == null) item { Text("No smart albums yet.") }
                items(vm.themes, key = { it.str("id") }) { t ->
                    Card(onClick = { nav.navigate("theme/${t.str("id")}") }) {
                        Column(Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
                            Row(verticalAlignment = Alignment.CenterVertically) {
                                Text(t.str("name"), style = MaterialTheme.typography.titleSmall, modifier = Modifier.weight(1f))
                                Text("hourly", style = MaterialTheme.typography.labelSmall, modifier = Modifier.padding(end = 8.dp))
                                Switch(checked = t.b("enabled") == true, onCheckedChange = { vm.setEnabled(t, it) })
                            }
                            Text("${themeSummary(t)} → album “${t.str("album")}”", style = MaterialTheme.typography.bodySmall)
                            val last = t.s("lastRun")?.let { lr ->
                                val when_ = runCatching { OffsetDateTime.parse(lr).format(DateTimeFormatter.ofLocalizedDateTime(FormatStyle.SHORT)) }.getOrDefault(lr)
                                "last run $when_: +${t.i("lastAdded") ?: 0} (${t.i("lastMatched")?.toString() ?: "?"} match)"
                            } ?: "never run"
                            Text(last + (t.s("lastError")?.let { " · ERROR: $it" } ?: ""), style = MaterialTheme.typography.labelSmall,
                                color = if (t.s("lastError") != null) MaterialTheme.colorScheme.error else MaterialTheme.colorScheme.onSurfaceVariant)
                            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                                FilledTonalButton(onClick = { vm.run(listOf(t.str("id"))) }) { Text("Run now") }
                                t.s("albumId")?.let { albumId ->
                                    TextButton(onClick = { nav.navigate("album/$albumId") }) { Text("Open album") }
                                }
                                TextButton(onClick = { deleting = t }) { Text("Delete", color = MaterialTheme.colorScheme.error) }
                            }
                        }
                    }
                }
            }
            BusyLayer(vm.busy)
        }
    }
    deleting?.let { t ->
        var alsoAlbum by remember { mutableStateOf(false) }
        androidx.compose.material3.AlertDialog(
            onDismissRequest = { deleting = null },
            title = { Text("Delete “${t.str("name")}”?") },
            text = {
                Column {
                    Text("The smart album stops; the photos it added stay where they are.")
                    if (t.s("albumId") != null) CheckRow("Also delete the album “${t.str("album")}” (photos stay in your library)", alsoAlbum) { alsoAlbum = it }
                }
            },
            confirmButton = { TextButton(onClick = { deleting = null; vm.delete(t, alsoAlbum) }) { Text("Delete", color = MaterialTheme.colorScheme.error) } },
            dismissButton = { TextButton(onClick = { deleting = null }) { Text("Cancel") } },
        )
    }
}
