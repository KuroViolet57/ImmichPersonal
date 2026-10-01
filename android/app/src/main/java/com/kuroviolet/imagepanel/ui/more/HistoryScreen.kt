package com.kuroviolet.imagepanel.ui.more

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
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material3.Card
import androidx.compose.material3.FilledTonalButton
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
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
import com.kuroviolet.imagepanel.net.i
import com.kuroviolet.imagepanel.net.o
import com.kuroviolet.imagepanel.net.obj
import com.kuroviolet.imagepanel.net.objects
import com.kuroviolet.imagepanel.net.str
import com.kuroviolet.imagepanel.net.strings
import com.kuroviolet.imagepanel.ui.albums.AlbumStore
import com.kuroviolet.imagepanel.ui.components.BusyLayer
import com.kuroviolet.imagepanel.ui.components.Confirm
import com.kuroviolet.imagepanel.ui.components.ConfirmDialog
import com.kuroviolet.imagepanel.ui.components.Hint
import com.kuroviolet.imagepanel.ui.components.ScreenTop
import kotlinx.coroutines.launch
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.intOrNull
import kotlinx.serialization.json.put
import java.time.OffsetDateTime
import java.time.format.DateTimeFormatter
import java.time.format.FormatStyle

class HistoryVm : ViewModel() {
    var runs by mutableStateOf<List<JsonObject>>(emptyList())
    var busy by mutableStateOf<String?>(null)

    fun load() {
        busy = "Loading…"
        viewModelScope.launch {
            try { runs = Graph.api.get("/api/history").obj().a("runs").objects() }
            catch (e: Exception) { UiBus.error(e, "history") } finally { busy = null }
        }
    }

    fun undo(runId: String) {
        busy = "Undoing…"
        viewModelScope.launch {
            try {
                val data = Graph.api.post("/api/undo", buildJsonObject { put("runId", runId) }).obj()
                val removed = data.i("removed") ?: 0
                val failures = data.a("failures").strings()
                UiBus.toast("Undone: took $removed out of albums, put ${data.i("restored") ?: 0} back." +
                    if (failures.isNotEmpty()) " ${failures.size} problem(s)." else "")
                RefData.loadAlbums()
                runCatching { AlbumStore.load() }
                load()
            } catch (e: Exception) {
                UiBus.error(e, "undo")
                busy = null
            }
        }
    }
}

private fun countMap(o: JsonObject): String =
    o.entries.joinToString(", ") { (k, v) -> "$k (${(v as? JsonPrimitive)?.intOrNull ?: 0})" }

@Composable
fun HistoryScreen(nav: NavController) {
    val vm: HistoryVm = viewModel()
    LaunchedEffect(Unit) { vm.load() }
    var confirm by remember { mutableStateOf<Confirm?>(null) }
    Scaffold(topBar = {
        ScreenTop("History", nav) { IconButton(onClick = { vm.load() }) { Icon(Icons.Filled.Refresh, "Refresh") } }
    }) { padding ->
        Box(Modifier.fillMaxSize().padding(padding)) {
            LazyColumn(contentPadding = PaddingValues(12.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                item {
                    Hint("Every add, move and smart-album run is recorded here. Undo removes the photos a change added and, " +
                        "for a move, puts them back in the albums they were taken out of. Albums are never deleted.")
                }
                items(vm.runs, key = { it.str("runId") }) { run ->
                    Card {
                        Column(Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
                            val time = run.str("timestamp").let { t ->
                                runCatching { OffsetDateTime.parse(t).format(DateTimeFormatter.ofLocalizedDateTime(FormatStyle.MEDIUM)) }.getOrDefault(t)
                            }
                            Text(time + run.str("note").let { if (it.isNotEmpty()) " · $it" else "" }, style = MaterialTheme.typography.titleSmall)
                            val added = run.o("added")
                            if (added.isNotEmpty()) Text("Added to: ${countMap(added)}", style = MaterialTheme.typography.bodySmall)
                            val removed = run.o("removed")
                            if (removed.isNotEmpty()) Text("Taken out of: ${countMap(removed)}", style = MaterialTheme.typography.bodySmall)
                            val deleted = run.a("deleted").strings()
                            if (deleted.isNotEmpty()) Text("Deleted albums: ${deleted.joinToString()}", style = MaterialTheme.typography.bodySmall)
                            val renamed = run.a("renamed").strings()
                            if (renamed.isNotEmpty()) Text("Renamed: ${renamed.joinToString()}", style = MaterialTheme.typography.bodySmall)
                            Row(verticalAlignment = Alignment.CenterVertically) {
                                if (run.b("undone") == true) {
                                    Text("Undone", color = MaterialTheme.colorScheme.onSurfaceVariant)
                                } else {
                                    FilledTonalButton(onClick = {
                                        confirm = Confirm("Undo this change?", "The photos it added are taken out again" +
                                            (if (removed.isNotEmpty()) ", and the ones it moved go back to their albums." else ".")) {
                                            vm.undo(run.str("runId"))
                                        }
                                    }) { Text("Undo") }
                                }
                            }
                        }
                    }
                }
            }
            BusyLayer(vm.busy)
        }
    }
    ConfirmDialog(confirm) { confirm = null }
}
