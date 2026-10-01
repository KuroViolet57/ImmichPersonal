package com.kuroviolet.imagepanel.ui.albums

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.grid.GridCells
import androidx.compose.foundation.lazy.grid.LazyVerticalGrid
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.MoreVert
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.FilledTonalButton
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
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
import androidx.compose.ui.platform.LocalFocusManager
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.unit.dp
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import androidx.lifecycle.viewmodel.compose.viewModel
import androidx.navigation.NavController
import com.kuroviolet.imagepanel.Graph
import com.kuroviolet.imagepanel.model.Asset
import com.kuroviolet.imagepanel.model.RefData
import com.kuroviolet.imagepanel.model.PanelPrefs
import com.kuroviolet.imagepanel.model.UiBus
import com.kuroviolet.imagepanel.model.ViewerSession
import com.kuroviolet.imagepanel.net.a
import com.kuroviolet.imagepanel.net.b
import com.kuroviolet.imagepanel.net.i
import com.kuroviolet.imagepanel.net.obj
import com.kuroviolet.imagepanel.net.s
import com.kuroviolet.imagepanel.net.str
import com.kuroviolet.imagepanel.net.strings
import com.kuroviolet.imagepanel.ui.components.BusyLayer
import com.kuroviolet.imagepanel.ui.components.Confirm
import com.kuroviolet.imagepanel.ui.components.ConfirmDialog
import com.kuroviolet.imagepanel.ui.components.Hint
import com.kuroviolet.imagepanel.ui.components.MEDIA_OPTIONS
import com.kuroviolet.imagepanel.ui.components.ScreenTop
import com.kuroviolet.imagepanel.ui.components.Segmented
import com.kuroviolet.imagepanel.ui.components.Selection
import com.kuroviolet.imagepanel.ui.components.TextPrompt
import com.kuroviolet.imagepanel.ui.components.albumOptions
import com.kuroviolet.imagepanel.ui.components.assetItems
import com.kuroviolet.imagepanel.ui.components.fullSpan
import kotlinx.coroutines.launch
import kotlinx.serialization.json.add
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import kotlinx.serialization.json.putJsonArray
import java.net.URLEncoder

val ALBUM_SORTS = listOf(
    "taken_desc" to "Date taken — newest first",
    "taken_asc" to "Date taken — oldest first",
    "added_desc" to "Added to album — newest first",
    "added_asc" to "Added to album — oldest first",
    "uploaded_desc" to "Uploaded — newest first",
    "uploaded_asc" to "Uploaded — oldest first",
    "name_asc" to "File name A–Z",
    "relevance" to "Most relevant to a description",
)

class AlbumVm : ViewModel() {
    var albumId by mutableStateOf("")
    var items by mutableStateOf<List<Asset>>(emptyList())
    var sort by mutableStateOf("taken_desc")
    var media by mutableStateOf("")
    var query by mutableStateOf("")
    val selection = Selection()
    var busy by mutableStateOf<String?>(null)
    var loadedOnce = false

    /** The chosen order sticks (here and in the web panel); "most relevant" needs a description, so it doesn't. */
    fun rememberSort(k: String) {
        PanelPrefs.albumSort = k
        viewModelScope.launch { PanelPrefs.save("albumSort", k) }
    }

    fun open(id: String) {
        if (albumId == id && loadedOnce) return
        albumId = id
        sort = PanelPrefs.albumSort; media = ""; query = ""
        load()
    }

    fun load() {
        val id = albumId
        busy = "Opening ${AlbumStore.byId(id)?.name ?: "album"}…"
        viewModelScope.launch {
            try {
                if (!AlbumStore.loaded) runCatching { AlbumStore.load() }
                var path = "/api/albums/$id/items?sort=$sort&type=$media"
                if (sort == "relevance") path += "&q=" + URLEncoder.encode(query.trim(), "UTF-8")
                val data = Graph.api.get(path).obj()
                items = Asset.list(data.a("items"))
                selection.clear()
                loadedOnce = true
                if (data.s("sort") == "relevance") {
                    query = data.str("query", query)
                    (data.i("unscored") ?: 0).takeIf { it > 0 }?.let { UiBus.toast("$it item(s) have no smart-search score yet — listed last.") }
                }
                if (data.b("addedAvailable") == false) UiBus.error("Date added isn't available (no database access) — sorted by date taken.")
            } catch (e: Exception) {
                UiBus.error(e, "album")
            } finally {
                busy = null
            }
        }
    }

    private fun act(message: String, block: suspend () -> Unit) {
        busy = message
        viewModelScope.launch {
            try { block() } catch (e: Exception) { UiBus.error(e, "album action") } finally { busy = null }
        }
    }

    fun transfer(target: String, move: Boolean) {
        val ids = selection.ids(items)
        act("${if (move) "Moving" else "Copying"} ${ids.size} to $target…") {
            val data = AlbumStore.action("transfer", buildJsonObject {
                put("sourceId", albumId); put("target", target); put("move", move)
                putJsonArray("assetIds") { ids.forEach { add(it) } }
            })
            UiBus.toast("${if (move) "Moved" else "Copied"} ${(data.i("added") ?: 0) + (data.i("alreadyThere") ?: 0)} to “${data.str("target")}”" +
                if (data.b("created") == true) " (new album)." else ".")
            load()
        }
    }

    fun remove() {
        val ids = selection.ids(items)
        act("Taking ${ids.size} out of the album…") {
            val data = AlbumStore.action("remove", buildJsonObject {
                put("albumId", albumId); putJsonArray("assetIds") { ids.forEach { add(it) } }
            })
            UiBus.toast("Took ${data.i("removed") ?: 0} out of the album. They stay in your library.")
            load()
        }
    }

    fun rename(name: String) = act("Renaming…") {
        AlbumStore.action("rename", buildJsonObject { put("albumId", albumId); put("name", name) })
        UiBus.toast("Renamed to “$name”.")
    }

    fun merge(target: String, deleteSources: Boolean, replace: Boolean, onMoved: (String) -> Unit) = act("Merging into $target…") {
        val data = AlbumStore.action("merge", buildJsonObject {
            putJsonArray("sourceIds") { add(albumId) }
            put("target", target); put("deleteSources", deleteSources); put("replaceTarget", replace)
        })
        UiBus.toast("Merged into “${data.str("target")}”: +${data.i("added") ?: 0}.")
        val deleted = data.a("deletedAlbums").strings()
        val myName = AlbumStore.byId(albumId)?.name
        if (deleted.isNotEmpty() && (myName == null || myName in deleted || AlbumStore.byId(albumId) == null)) onMoved(data.str("targetId"))
    }

    fun deleteAlbum(onDone: () -> Unit) = act("Deleting the album…") {
        AlbumStore.action("delete", buildJsonObject { putJsonArray("albumIds") { add(albumId) } })
        UiBus.toast("Album deleted. The photos stay in your library.")
        onDone()
    }
}

@Composable
fun AlbumScreen(nav: NavController, albumId: String) {
    val vm: AlbumVm = viewModel(key = "album-$albumId")
    val focus = LocalFocusManager.current
    LaunchedEffect(albumId) { vm.open(albumId) }
    val album = AlbumStore.byId(albumId)
    var menu by remember { mutableStateOf(false) }
    var sortMenu by remember { mutableStateOf(false) }
    var transferPrompt by remember { mutableStateOf<Boolean?>(null) }   // true = move, false = copy
    var renamePrompt by remember { mutableStateOf(false) }
    var mergePrompt by remember { mutableStateOf(false) }
    var confirm by remember { mutableStateOf<Confirm?>(null) }
    Scaffold(
        topBar = {
            ScreenTop(album?.name ?: "Album", nav, subtitle = "${vm.items.size} items · ${vm.selection.size} selected") {
                Box {
                    IconButton(onClick = { menu = true }) { Icon(Icons.Filled.MoreVert, contentDescription = "More") }
                    DropdownMenu(expanded = menu, onDismissRequest = { menu = false }) {
                        DropdownMenuItem(text = { Text("Select all") }, onClick = { menu = false; vm.selection.selectAll(vm.items.map { it.id }) })
                        DropdownMenuItem(text = { Text("Select none") }, onClick = { menu = false; vm.selection.clear() })
                        DropdownMenuItem(text = { Text("Rename album…") }, onClick = { menu = false; renamePrompt = true })
                        DropdownMenuItem(text = { Text("Merge into another album…") }, onClick = { menu = false; mergePrompt = true })
                        DropdownMenuItem(text = { Text("Delete album…") }, onClick = {
                            menu = false
                            confirm = Confirm("Delete “${album?.name}”?", "Only the album is deleted — its photos stay in your library. Undo from History.",
                                "Delete", danger = true) { vm.deleteAlbum { nav.popBackStack() } }
                        })
                    }
                }
            }
        },
    ) { padding ->
        Box(Modifier.fillMaxSize().padding(padding)) {
            LazyVerticalGrid(
                columns = GridCells.Adaptive(Graph.settings.tileDp.dp),
                contentPadding = PaddingValues(start = 10.dp, end = 10.dp, bottom = 90.dp),
                horizontalArrangement = Arrangement.spacedBy(4.dp),
                verticalArrangement = Arrangement.spacedBy(4.dp),
                modifier = Modifier.fillMaxSize(),
            ) {
                fullSpan("controls") {
                    Column(verticalArrangement = Arrangement.spacedBy(8.dp), modifier = Modifier.padding(bottom = 6.dp)) {
                        album?.description?.takeIf { it.isNotEmpty() }?.let { Hint(it) }
                        Box {
                            OutlinedButton(onClick = { sortMenu = true }, modifier = Modifier.fillMaxWidth()) {
                                Text("Sort: " + (ALBUM_SORTS.firstOrNull { it.first == vm.sort }?.second ?: vm.sort))
                            }
                            DropdownMenu(expanded = sortMenu, onDismissRequest = { sortMenu = false }) {
                                ALBUM_SORTS.forEach { (k, label) ->
                                    DropdownMenuItem(text = { Text(label) }, onClick = {
                                        sortMenu = false
                                        vm.sort = k
                                        if (k != "relevance") vm.rememberSort(k)
                                        if (k != "relevance") vm.load() else if (vm.query.isNotBlank()) vm.load()
                                    })
                                }
                            }
                        }
                        if (vm.sort == "relevance") {
                            OutlinedTextField(
                                value = vm.query, onValueChange = { vm.query = it },
                                label = { Text("What should the photos look like?") },
                                placeholder = { Text("empty = the smart album's description or the album name") },
                                singleLine = true, modifier = Modifier.fillMaxWidth(),
                                keyboardOptions = KeyboardOptions(imeAction = ImeAction.Search),
                                keyboardActions = KeyboardActions(onSearch = { focus.clearFocus(); vm.load() }),
                            )
                            TextButton(onClick = { focus.clearFocus(); vm.load() }) { Text("Sort by this") }
                        }
                        Segmented(MEDIA_OPTIONS, vm.media, { vm.media = it; vm.load() })
                        if (vm.selection.size > 0) {
                            Row(horizontalArrangement = Arrangement.spacedBy(6.dp), verticalAlignment = Alignment.CenterVertically) {
                                FilledTonalButton(onClick = { transferPrompt = false }) { Text("Copy to…") }
                                FilledTonalButton(onClick = { transferPrompt = true }) { Text("Move to…") }
                                FilledTonalButton(onClick = {
                                    confirm = Confirm("Take ${vm.selection.size} out of this album?",
                                        "They stay in your library and in any other albums.", "Take out") { vm.remove() }
                                }) { Text("Remove") }
                            }
                        } else {
                            Hint("Tap to open; tap the circle (or long-press) to select for copying, moving or removing.")
                        }
                    }
                }
                assetItems(
                    vm.items, vm.selection,
                    label = { _, a -> a.score?.let { "%.3f".format(it) } },
                ) { index ->
                    ViewerSession.open(vm.items, index, vm.selection, album?.name ?: "Album")
                    nav.navigate("viewer")
                }
            }
            BusyLayer(vm.busy)
        }
    }
    transferPrompt?.let { move ->
        TextPrompt(
            title = "${if (move) "Move" else "Copy"} ${vm.selection.size} to…", initial = "", label = "Album (existing or new)",
            yes = if (move) "Move" else "Copy", options = albumOptions(RefData.albums), onClose = { transferPrompt = null },
        ) { vm.transfer(it, move) }
    }
    if (renamePrompt) {
        TextPrompt("Rename album", album?.name ?: "", "New name", "Rename", onClose = { renamePrompt = false }) { vm.rename(it) }
    }
    if (mergePrompt) {
        MergeDialog(listOf(albumId), onClose = { mergePrompt = false }) { target, del, replace ->
            vm.merge(target, del, replace) { newId ->
                nav.popBackStack()
                if (newId.isNotEmpty()) nav.navigate("album/$newId")
            }
        }
    }
    ConfirmDialog(confirm) { confirm = null }
}
