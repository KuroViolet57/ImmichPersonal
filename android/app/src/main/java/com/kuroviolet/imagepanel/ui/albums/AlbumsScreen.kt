@file:OptIn(ExperimentalFoundationApi::class)

package com.kuroviolet.imagepanel.ui.albums

import androidx.compose.foundation.ExperimentalFoundationApi
import androidx.compose.foundation.background
import androidx.compose.foundation.combinedClickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Add
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material.icons.filled.Sort
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Checkbox
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.FilledTonalButton
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateMapOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import androidx.lifecycle.viewmodel.compose.viewModel
import androidx.navigation.NavController
import coil3.compose.AsyncImage
import com.kuroviolet.imagepanel.Graph
import com.kuroviolet.imagepanel.model.Album
import com.kuroviolet.imagepanel.model.RefData
import com.kuroviolet.imagepanel.model.PanelPrefs
import com.kuroviolet.imagepanel.model.UiBus
import com.kuroviolet.imagepanel.net.a
import com.kuroviolet.imagepanel.net.b
import com.kuroviolet.imagepanel.net.i
import com.kuroviolet.imagepanel.net.obj
import com.kuroviolet.imagepanel.net.objects
import com.kuroviolet.imagepanel.net.s
import com.kuroviolet.imagepanel.net.str
import com.kuroviolet.imagepanel.net.strings
import com.kuroviolet.imagepanel.ui.components.BusyLayer
import com.kuroviolet.imagepanel.ui.components.CheckRow
import com.kuroviolet.imagepanel.ui.components.Confirm
import com.kuroviolet.imagepanel.ui.components.ConfirmDialog
import com.kuroviolet.imagepanel.ui.components.Hint
import com.kuroviolet.imagepanel.ui.components.ScreenTop
import com.kuroviolet.imagepanel.ui.components.SuggestField
import com.kuroviolet.imagepanel.ui.components.TextPrompt
import com.kuroviolet.imagepanel.ui.components.albumOptions
import com.kuroviolet.imagepanel.ui.components.plural
import kotlinx.coroutines.launch
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.add
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import kotlinx.serialization.json.putJsonArray

/** All albums with their details (GET /api/albums/list), shared by the album screens. */
object AlbumStore {
    var albums by mutableStateOf<List<Album>>(emptyList())
        private set
    var loaded by mutableStateOf(false)
        private set

    suspend fun load() {
        val data = Graph.api.get("/api/albums/list").obj()
        albums = data.a("albums").objects().map {
            Album(
                id = it.str("id"), name = it.str("name"), count = it.i("count") ?: 0, thumb = it.s("thumb"),
                description = it.str("description"), shared = it.b("shared") == true, updatedAt = it.str("updatedAt"),
            )
        }
        loaded = true
    }

    fun byId(id: String) = albums.firstOrNull { it.id == id }

    /** Runs an album action (POST /api/albums/{action}) and refreshes every album list. */
    suspend fun action(action: String, body: JsonObject): JsonObject {
        val data = Graph.api.post("/api/albums/$action", body).obj()
        val failures = data.a("failures").strings()
        if (failures.isNotEmpty()) UiBus.error("${failures.size} problem(s): ${failures.first()}")
        runCatching { load() }
        RefData.loadAlbums()
        return data
    }
}

class AlbumsVm : ViewModel() {
    var filter by mutableStateOf("")
    val sort: String get() = PanelPrefs.albumsSort

    fun chooseSort(k: String) {
        PanelPrefs.albumsSort = k
        viewModelScope.launch { PanelPrefs.save("albumsSort", k) }
    }
    val picked = mutableStateMapOf<String, Boolean>()
    var busy by mutableStateOf<String?>(null)

    fun load(force: Boolean = false) {
        if (AlbumStore.loaded && !force) return
        busy = "Loading albums…"
        viewModelScope.launch {
            try { AlbumStore.load() } catch (e: Exception) { UiBus.error(e, "albums") } finally { busy = null }
        }
    }

    fun visible(): List<Album> {
        val q = filter.trim().lowercase()
        val list = AlbumStore.albums.filter { q.isEmpty() || it.name.lowercase().contains(q) || it.description.lowercase().contains(q) }
        return when (sort) {
            "big" -> list.sortedWith(compareByDescending<Album> { it.count }.thenBy { it.name.lowercase() })
            "small" -> list.sortedWith(compareBy<Album> { it.count }.thenBy { it.name.lowercase() })
            "updated" -> list.sortedByDescending { it.updatedAt }
            else -> list.sortedWith(compareBy(String.CASE_INSENSITIVE_ORDER) { it.name })
        }
    }

    fun run(message: String, block: suspend () -> Unit) {
        busy = message
        viewModelScope.launch {
            try { block() } catch (e: Exception) { UiBus.error(e, "albums") } finally { busy = null }
        }
    }

    fun create(name: String) = run("Creating $name…") {
        val data = AlbumStore.action("create", buildJsonObject { put("name", name) })
        UiBus.toast("Created “${data.str("name", name)}”.")
    }

    fun merge(ids: List<String>, target: String, deleteSources: Boolean, replaceTarget: Boolean) = run("Merging into $target…") {
        val data = AlbumStore.action("merge", buildJsonObject {
            putJsonArray("sourceIds") { ids.forEach { add(it) } }
            put("target", target)
            put("deleteSources", deleteSources)
            put("replaceTarget", replaceTarget)
        })
        val deleted = data.a("deletedAlbums").strings()
        UiBus.toast("Merged into “${data.str("target")}”: +${data.i("added") ?: 0}" +
            (if (deleted.isNotEmpty()) ", deleted ${deleted.size} album(s)" else "") +
            ((data.i("removedFromTarget") ?: 0).takeIf { it > 0 }?.let { ", replaced $it" } ?: ""))
        picked.clear()
    }

    fun delete(ids: List<String>) = run("Deleting ${ids.size} album(s)…") {
        val data = AlbumStore.action("delete", buildJsonObject { putJsonArray("albumIds") { ids.forEach { add(it) } } })
        UiBus.toast("Deleted ${data.a("deleted").size} album(s). The photos themselves are kept.")
        picked.clear()
    }
}

@Composable
fun AlbumsScreen(nav: NavController) {
    val vm: AlbumsVm = viewModel()
    LaunchedEffect(Unit) { vm.load() }
    var sortMenu by remember { mutableStateOf(false) }
    var createPrompt by remember { mutableStateOf(false) }
    var mergePrompt by remember { mutableStateOf(false) }
    var confirm by remember { mutableStateOf<Confirm?>(null) }
    val list = vm.visible()
    Scaffold(
        topBar = {
            ScreenTop("Albums", subtitle = "${AlbumStore.albums.size} albums") {
                IconButton(onClick = { createPrompt = true }) { Icon(Icons.Filled.Add, contentDescription = "New album") }
                Box {
                    IconButton(onClick = { sortMenu = true }) { Icon(Icons.Filled.Sort, contentDescription = "Sort") }
                    DropdownMenu(expanded = sortMenu, onDismissRequest = { sortMenu = false }) {
                        listOf("name" to "Name A–Z", "big" to "Most items", "small" to "Fewest items", "updated" to "Recently changed").forEach { (k, label) ->
                            DropdownMenuItem(text = { Text((if (vm.sort == k) "✓ " else "") + label) }, onClick = { vm.chooseSort(k); sortMenu = false })
                        }
                    }
                }
                IconButton(onClick = { vm.load(force = true) }) { Icon(Icons.Filled.Refresh, contentDescription = "Refresh") }
            }
        },
    ) { padding ->
        Box(Modifier.fillMaxSize().padding(padding)) {
            LazyColumn(contentPadding = PaddingValues(start = 12.dp, end = 12.dp, bottom = 90.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
                item {
                    OutlinedTextField(
                        value = vm.filter, onValueChange = { vm.filter = it }, label = { Text("Find an album") },
                        singleLine = true, modifier = Modifier.fillMaxWidth(),
                    )
                    Hint("Showing ${list.size} of ${AlbumStore.albums.size} · ${AlbumStore.albums.count { it.count <= 3 }} have 3 items or fewer. " +
                        "Long-press albums to merge or delete several.", Modifier.padding(vertical = 6.dp))
                }
                if (vm.picked.isNotEmpty()) {
                    item {
                        Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                            Text("${vm.picked.size} selected", modifier = Modifier.weight(1f))
                            FilledTonalButton(onClick = { mergePrompt = true }) { Text("Merge…") }
                            FilledTonalButton(onClick = {
                                val ids = vm.picked.keys.toList()
                                confirm = Confirm("Delete ${ids.size} album(s)?", "Only the albums are deleted — the photos stay in your library. " +
                                    "Undo from History brings the albums back.", "Delete", danger = true) { vm.delete(ids) }
                            }) { Text("Delete") }
                            TextButton(onClick = { vm.picked.clear() }) { Text("Clear") }
                        }
                    }
                }
                items(list, key = { it.id }) { album ->
                    AlbumRow(
                        album, picked = vm.picked.containsKey(album.id),
                        onOpen = { if (vm.picked.isNotEmpty()) toggle(vm, album.id) else nav.navigate("album/${album.id}") },
                        onPick = { toggle(vm, album.id) },
                    )
                }
            }
            BusyLayer(vm.busy)
        }
    }
    if (createPrompt) {
        TextPrompt("New album", "", "Album name", "Create", onClose = { createPrompt = false }) { vm.create(it) }
    }
    if (mergePrompt) {
        MergeDialog(vm.picked.keys.toList(), onClose = { mergePrompt = false }) { target, del, replace ->
            vm.merge(vm.picked.keys.toList(), target, del, replace)
        }
    }
    ConfirmDialog(confirm) { confirm = null }
}

private fun toggle(vm: AlbumsVm, id: String) {
    if (vm.picked.containsKey(id)) vm.picked.remove(id) else vm.picked[id] = true
}

@Composable
private fun AlbumRow(album: Album, picked: Boolean, onOpen: () -> Unit, onPick: () -> Unit) {
    Row(
        Modifier.fillMaxWidth().clip(RoundedCornerShape(10.dp))
            .background(if (picked) MaterialTheme.colorScheme.secondaryContainer else MaterialTheme.colorScheme.surfaceContainerLow)
            .combinedClickable(onClick = onOpen, onLongClick = onPick).padding(8.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Box(Modifier.size(56.dp).clip(RoundedCornerShape(8.dp)).background(MaterialTheme.colorScheme.surfaceVariant)) {
            album.thumb?.let { AsyncImage(model = Graph.api.thumbUrl(it), contentDescription = null, contentScale = ContentScale.Crop, modifier = Modifier.fillMaxSize()) }
        }
        Spacer(Modifier.width(12.dp))
        Column(Modifier.weight(1f)) {
            Text(album.name.ifEmpty { "(untitled)" }, style = MaterialTheme.typography.titleSmall, maxLines = 1, overflow = TextOverflow.Ellipsis)
            Text(album.count.plural("item") + if (album.shared) " · shared" else "", style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
        Checkbox(checked = picked, onCheckedChange = { onPick() })
    }
}

/** Merge several albums into one (existing or new). */
@Composable
fun MergeDialog(ids: List<String>, onClose: () -> Unit, onYes: (String, Boolean, Boolean) -> Unit) {
    var target by remember { mutableStateOf("") }
    var deleteSources by remember { mutableStateOf(true) }
    var replace by remember { mutableStateOf(false) }
    val existing = AlbumStore.albums.firstOrNull { it.name.equals(target.trim(), true) }
    val names = ids.mapNotNull { AlbumStore.byId(it)?.name }
    AlertDialog(
        onDismissRequest = onClose,
        title = { Text("Merge ${ids.size} album(s)") },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(6.dp)) {
                Hint(names.take(8).joinToString(", ") + if (names.size > 8) ", …" else "")
                SuggestField(value = target, onValueChange = { target = it }, label = "Into which album? (existing or new)",
                    options = albumOptions(RefData.albums), onPick = { target = it })
                CheckRow("Delete the merged albums afterwards (photos are kept in the target)", deleteSources) { deleteSources = it }
                if (existing != null && existing.count > 0) {
                    CheckRow("Replace what “${existing.name}” has now (it ends up with only the merged photos)", replace) { replace = it }
                }
            }
        },
        confirmButton = { TextButton(enabled = target.isNotBlank(), onClick = { onClose(); onYes(target.trim(), deleteSources, replace) }) { Text("Merge") } },
        dismissButton = { TextButton(onClick = onClose) { Text("Cancel") } },
    )
}
