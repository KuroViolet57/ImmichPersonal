@file:OptIn(ExperimentalMaterial3Api::class)

package com.kuroviolet.imagepanel.ui.splus

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.lazy.grid.GridCells
import androidx.compose.foundation.lazy.grid.LazyVerticalGrid
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.PhotoAlbum
import androidx.compose.material.icons.filled.Storage
import androidx.compose.material3.Button
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.ExtendedFloatingActionButton
import androidx.compose.material3.FilledTonalButton
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.ModalBottomSheet
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.PrimaryTabRow
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Slider
import androidx.compose.material3.Switch
import androidx.compose.material3.Tab
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.rememberModalBottomSheetState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableFloatStateOf
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import com.kuroviolet.imagepanel.ui.more.ThemeDraft
import androidx.compose.ui.platform.LocalFocusManager
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.unit.dp
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import androidx.lifecycle.viewmodel.compose.viewModel
import androidx.navigation.NavController
import coil3.compose.AsyncImage
import com.kuroviolet.imagepanel.Graph
import com.kuroviolet.imagepanel.model.Asset
import com.kuroviolet.imagepanel.model.Ops
import com.kuroviolet.imagepanel.model.Pending
import com.kuroviolet.imagepanel.model.UiBus
import com.kuroviolet.imagepanel.model.ViewerSession
import com.kuroviolet.imagepanel.net.a
import com.kuroviolet.imagepanel.net.b
import com.kuroviolet.imagepanel.net.d
import com.kuroviolet.imagepanel.net.i
import com.kuroviolet.imagepanel.net.o
import com.kuroviolet.imagepanel.net.obj
import com.kuroviolet.imagepanel.net.objects
import com.kuroviolet.imagepanel.net.s
import com.kuroviolet.imagepanel.net.str
import com.kuroviolet.imagepanel.ui.components.BusyLayer
import com.kuroviolet.imagepanel.ui.components.CheckRow
import com.kuroviolet.imagepanel.ui.components.Confirm
import com.kuroviolet.imagepanel.ui.components.ConfirmDialog
import com.kuroviolet.imagepanel.ui.components.DateField
import com.kuroviolet.imagepanel.ui.components.FileRequest
import com.kuroviolet.imagepanel.ui.components.FileSheet
import com.kuroviolet.imagepanel.ui.components.Hint
import com.kuroviolet.imagepanel.ui.components.MEDIA_OPTIONS
import com.kuroviolet.imagepanel.ui.components.NumberField
import com.kuroviolet.imagepanel.ui.components.ScreenTop
import com.kuroviolet.imagepanel.ui.components.SectionCard
import com.kuroviolet.imagepanel.ui.components.Segmented
import com.kuroviolet.imagepanel.ui.components.Selection
import com.kuroviolet.imagepanel.ui.components.TagFilterFields
import com.kuroviolet.imagepanel.ui.components.TagFilterState
import com.kuroviolet.imagepanel.ui.components.UnloadLine
import com.kuroviolet.imagepanel.ui.components.assetItems
import com.kuroviolet.imagepanel.ui.components.fullSpan
import com.kuroviolet.imagepanel.ui.components.plural
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import kotlinx.serialization.json.putJsonObject

class SearchPlusVm : ViewModel() {
    var mode by mutableStateOf("text")
    var text by mutableStateOf("")
    var like by mutableStateOf("")
    var type by mutableStateOf("")
    var limit by mutableIntStateOf(200)
    var after by mutableStateOf("")
    var before by mutableStateOf("")
    var compare by mutableStateOf(false)
    var showFilters by mutableStateOf(false)
    /** AI tags and description text: the search ranks only the photos that match them (or lists them, with no text). */
    val tagFilter = TagFilterState()

    var results by mutableStateOf<List<Asset>>(emptyList())
    var immich by mutableStateOf<List<Asset>>(emptyList())
    var immichModel by mutableStateOf("")
    var overlap by mutableIntStateOf(0)
    var compared by mutableStateOf(false)
    var tab by mutableIntStateOf(0)
    var note by mutableStateOf<String?>(null)
    val selection = Selection()
    var busy by mutableStateOf<String?>(null)
    var status by mutableStateOf<JsonObject?>(null)

    fun refresh() {
        viewModelScope.launch {
            status = runCatching { Graph.api.get("/api/searchplus").obj() }.getOrElse {
                if (status == null) UiBus.error(it, "search+ status")
                status
            }
        }
    }

    fun search() {
        if (mode == "text" && text.isBlank() && !tagFilter.active) {
            return UiBus.error("Type what you are looking for, or filter by AI tags or description text.")
        }
        if (mode == "like" && !Regex("^[0-9a-fA-F-]{36}$").matches(like.trim())) {
            return UiBus.error("Pick a photo: open any photo and press “Similar”, or paste its ID.")
        }
        val body = buildJsonObject {
            if (mode == "text") put("text", text.trim()) else put("like", like.trim())
            tagFilter.putInto(this)
            if (type.isNotEmpty()) put("media", type)
            put("limit", limit.coerceAtMost(1000))
            if (after.isNotEmpty()) put("after", after)
            if (before.isNotEmpty()) put("before", before)
            put("compare", compare)
        }
        val loaded = status?.o("service")?.s("status") == "ok"
        busy = if (mode == "text" && text.isBlank()) "Looking up the photos with those tags or that description…"
        else if (mode == "text" && !loaded) "Loading the Search+ model, then searching… (about half a minute)" else "Searching…"
        viewModelScope.launch {
            try {
                val data = Graph.api.post("/api/searchplus/search", body).obj()
                results = Asset.list(data.a("assets"))
                selection.selectAll(results.map { it.id })
                val im = data["immich"] as? JsonObject
                compared = im != null
                immich = im?.let { Asset.list(it.a("assets")) } ?: emptyList()
                immichModel = im?.str("model") ?: ""
                overlap = im?.i("overlap") ?: 0
                tab = 0
                val c = data.o("counts")
                val indexed = c.i("indexed") ?: 0
                val assets = c.i("assets") ?: 0
                val pending = (c.i("pending") ?: 0) + (c.i("retrying") ?: 0)
                val filters = data.o("filters")
                tagFilter.summary = filters.s("text")
                val listed = filters.s("ranking") == "none"          // tags / description only: listed newest first, nothing ranked
                note = when {
                    listed -> "Took %.1f s. Tap a photo to open it; the circle ticks it.".format((data.i("tookMs") ?: 0) / 1000.0) +
                        (data.i("total")?.takeIf { it > results.size }?.let { " %,d match in all: raise Max results to see more.".format(it) } ?: "")
                    indexed == 0 -> "The index is empty — press the database icon at the top, then “Build index”."
                    pending > 0 -> "Searched the %,d of %,d items indexed so far — the index is still being built.".format(indexed, assets)
                    else -> "Took %.1f s. Videos score by their best-matching frame.".format((data.i("tookMs") ?: 0) / 1000.0)
                }
                refresh()
            } catch (e: Exception) {
                UiBus.error(e, "search+")
            } finally {
                busy = null
            }
        }
    }

    /** "Save as smart album…": the same question as a smart album scored with the Search+ model. */
    fun draftSmartAlbum(): Boolean {
        if (mode == "text" && text.isBlank() || mode == "like" && like.isBlank()) {
            UiBus.error("Set up a search first.")
            return false
        }
        val name = (if (mode == "text") text.trim() else "Similar photos").take(40)
        ThemeDraft.prefill = buildJsonObject {
            put("source", if (mode == "like") "like" else "text")
            put("engine", "searchplus")
            put("description", if (mode == "text") text.trim() else "")
            put("like", if (mode == "like") like.trim() else "")
            put("name", name); put("album", name)
            put("mode", "top"); put("limit", limit); put("enabled", false); put("cutoff", 0.17)
            put("media", type)
            put("taken_after", after); put("taken_before", before)
        }
        UiBus.toast("Filled in from your Search+ search as “the best N”. Preview, adjust, then Save." +
            if (tagFilter.active) " The AI tag and description filters are not part of a smart album, so they are left out." else "")
        return true
    }

    fun file(req: FileRequest) {
        val ids = selection.ids(results)
        busy = "Adding ${ids.size} to ${req.album}…"
        viewModelScope.launch {
            try {
                UiBus.toast(Ops.file(ids, req))
            } catch (e: Exception) {
                UiBus.error(e, "file")
            } finally {
                busy = null
            }
        }
    }

    fun action(path: String, body: JsonObject, message: String? = null, onDone: (JsonElement) -> Unit = {}) {
        viewModelScope.launch {
            try {
                val data = Graph.api.post("/api/searchplus/$path", body)
                status = data.obj()
                message?.let { UiBus.toast(it) }
                onDone(data)
            } catch (e: Exception) {
                UiBus.error(e, "search+ $path")
            }
        }
    }
}

@Composable
fun SearchPlusScreen(nav: NavController) {
    val vm: SearchPlusVm = viewModel()
    var fileSheet by remember { mutableStateOf(false) }
    var indexSheet by remember { mutableStateOf(false) }
    LaunchedEffect(Unit) {
        while (true) {
            vm.refresh()
            delay(5000)
        }
    }
    LaunchedEffect(Pending.searchPlusLike) {
        Pending.searchPlusLike?.let { id ->
            Pending.searchPlusLike = null
            vm.mode = "like"
            vm.like = id
            vm.search()
        }
    }
    Scaffold(
        topBar = {
            ScreenTop("Search+", subtitle = "PE-Core G/14 · experimental") {
                IconButton(onClick = { indexSheet = true }) { Icon(Icons.Filled.Storage, contentDescription = "Index") }
            }
        },
        floatingActionButton = {
            if (vm.selection.size > 0 && vm.results.isNotEmpty() && vm.tab == 0) {
                ExtendedFloatingActionButton(
                    onClick = { fileSheet = true },
                    icon = { Icon(Icons.Filled.PhotoAlbum, contentDescription = null) },
                    text = { Text("Album… (${vm.selection.size})") },
                )
            }
        },
    ) { padding ->
        Box(Modifier.fillMaxSize().padding(padding)) {
            val mineIds = remember(vm.results) { vm.results.map { it.id }.toHashSet() }
            val theirIds = remember(vm.immich) { vm.immich.map { it.id }.toHashSet() }
            LazyVerticalGrid(
                columns = GridCells.Adaptive(Graph.settings.tileDp.dp),
                contentPadding = PaddingValues(start = 10.dp, end = 10.dp, top = 4.dp, bottom = 96.dp),
                horizontalArrangement = Arrangement.spacedBy(4.dp),
                verticalArrangement = Arrangement.spacedBy(4.dp),
                modifier = Modifier.fillMaxSize(),
            ) {
                fullSpan("form") { Form(vm) { if (vm.draftSmartAlbum()) nav.navigate("theme/new") } }
                fullSpan("status") { StatusLine(vm.status) { indexSheet = true } }
                if (vm.results.isNotEmpty() || vm.note != null) {
                    fullSpan("head") {
                        Column(Modifier.padding(top = 8.dp)) {
                            if (vm.compared) {
                                PrimaryTabRow(selectedTabIndex = vm.tab) {
                                    Tab(selected = vm.tab == 0, onClick = { vm.tab = 0 }, text = { Text("Search+ (${vm.results.size})") })
                                    Tab(selected = vm.tab == 1, onClick = { vm.tab = 1 }, text = { Text("Immich (${vm.immich.size})") })
                                }
                                Hint("${vm.overlap} found by both (marked “both”). Immich model: ${vm.immichModel}", Modifier.padding(top = 6.dp))
                            }
                            if (vm.tab == 0) {
                                Row(verticalAlignment = Alignment.CenterVertically) {
                                    Text("${vm.results.size.plural("result")} · ${vm.selection.size} selected",
                                        style = MaterialTheme.typography.titleSmall, modifier = Modifier.weight(1f))
                                    TextButton(onClick = { vm.selection.selectAll(vm.results.map { it.id }) }) { Text("All") }
                                    TextButton(onClick = { vm.selection.clear() }) { Text("None") }
                                }
                            }
                            vm.tagFilter.summary?.let { Hint(it) }
                            vm.note?.let { Hint(it) }
                        }
                    }
                }
                if (vm.tab == 0) {
                    assetItems(
                        vm.results, vm.selection,
                        badge = { a -> if (vm.compared && a.id in theirIds) "both" else null },
                    ) { index ->
                        ViewerSession.open(vm.results, index, vm.selection, "Search+")
                        nav.navigate("viewer")
                    }
                } else {
                    assetItems(
                        vm.immich, null,
                        badge = { a -> if (a.id in mineIds) "both" else null },
                    ) { index ->
                        ViewerSession.open(vm.immich, index, null, "Immich")
                        nav.navigate("viewer")
                    }
                }
            }
            BusyLayer(vm.busy)
        }
    }
    if (fileSheet) FileSheet(count = vm.selection.size, showSkip = false, onClose = { fileSheet = false }, onConfirm = { vm.file(it) })
    if (indexSheet) IndexSheet(vm) { indexSheet = false }
}

@Composable
private fun Form(vm: SearchPlusVm, onSaveAsSmart: () -> Unit) {
    val focus = LocalFocusManager.current
    SectionCard {
        Hint("A second “describe it” search with a bigger AI model: PE-Core G/14 (Meta's Perception Encoder) instead of " +
            "Immich's SigLIP2. Videos and GIFs are indexed on several frames. It has its own index; nothing in Immich changes.")
        Segmented(listOf("text" to "Describe it", "like" to "More like a photo"), vm.mode, { vm.mode = it })
        if (vm.mode == "text") {
            OutlinedTextField(
                value = vm.text, onValueChange = { vm.text = it },
                label = { Text("What are you looking for?") }, placeholder = { Text("two people hiking at sunset") },
                singleLine = true, modifier = Modifier.fillMaxWidth(),
                keyboardOptions = KeyboardOptions(imeAction = ImeAction.Search),
                keyboardActions = KeyboardActions(onSearch = { focus.clearFocus(); vm.search() }),
            )
        } else {
            Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                if (vm.like.length == 36) {
                    AsyncImage(model = Graph.api.thumbUrl(vm.like), contentDescription = null, modifier = Modifier.size(56.dp))
                }
                OutlinedTextField(
                    value = vm.like, onValueChange = { vm.like = it.trim() },
                    label = { Text("The photo to match (asset ID)") }, singleLine = true, modifier = Modifier.weight(1f),
                )
            }
            Hint("Easiest: open any photo and press “Similar”.")
        }
        Segmented(MEDIA_OPTIONS, vm.type, { vm.type = it })
        TextButton(onClick = { vm.showFilters = !vm.showFilters }) {
            Text((if (vm.showFilters) "Hide filters" else "More filters") + if (vm.tagFilter.active) " · tags or description set" else "")
        }
        if (vm.showFilters) {
            NumberField("Max results (up to 1,000)", vm.limit, { vm.limit = it }, Modifier.fillMaxWidth(), max = 1000)
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                DateField("Taken after", vm.after, { vm.after = it }, Modifier.weight(1f))
                DateField("Taken before", vm.before, { vm.before = it }, Modifier.weight(1f))
            }
            TagFilterFields(
                vm.tagFilter,
                "The tags the AI Tagger found. Only photos with them are ranked (and only their pictures are looked at); " +
                    "leave the text empty to just list them, newest first.",
            )
        }
        CheckRow("Compare with Immich's own smart search", vm.compare) { vm.compare = it }
        Button(onClick = { focus.clearFocus(); vm.search() }, modifier = Modifier.fillMaxWidth()) { Text("Search") }
        TextButton(onClick = onSaveAsSmart, modifier = Modifier.fillMaxWidth()) { Text("Save as smart album…") }
    }
}

@Composable
private fun StatusLine(status: JsonObject?, onOpen: () -> Unit) {
    status ?: return
    val c = status.o("counts")
    val sv = status.o("service")
    val ix = status.o("indexer")
    val model = when {
        sv.s("status") == "ok" -> "model loaded"
        sv.s("container") == "running" -> "model loading"
        else -> "model not loaded (GPU free)"
    }
    val state = indexerText(ix, c)
    TextButton(onClick = onOpen, modifier = Modifier.fillMaxWidth()) {
        Text("Index: %,d of %,d · %s · %s".format(c.i("indexed") ?: 0, c.i("assets") ?: 0, state, model),
            style = MaterialTheme.typography.bodySmall)
    }
}

private fun indexerText(ix: JsonObject, c: JsonObject): String = when (ix.s("state")) {
    "running" -> ix.d("ratePerMin")?.takeIf { it > 0 }?.let { "indexing %,d/min".format(it.toInt()) } ?: "indexing"
    "starting" -> ix.str("detail", "starting")
    "done" -> "up to date"
    "error" -> "stopped: ${ix.str("detail")}"
    else -> if ((c.i("indexed") ?: 0) > 0) "paused" else "not built yet"
}

@Composable
private fun IndexSheet(vm: SearchPlusVm, onClose: () -> Unit) {
    val sheet = rememberModalBottomSheetState(skipPartiallyExpanded = true)
    var confirm by remember { mutableStateOf<Confirm?>(null) }
    ModalBottomSheet(onDismissRequest = onClose, sheetState = sheet) {
        val st = vm.status
        Column(
            Modifier.fillMaxWidth().verticalScroll(rememberScrollState()).padding(horizontal = 18.dp).padding(bottom = 18.dp).navigationBarsPadding(),
            verticalArrangement = Arrangement.spacedBy(10.dp),
        ) {
            Text("Search+ index", style = MaterialTheme.typography.titleLarge)
            if (st == null) {
                Text("Loading…")
                return@Column
            }
            val c = st.o("counts")
            val ix = st.o("indexer")
            val sv = st.o("service")
            val cfg = st.o("settings")
            val indexing = cfg.b("indexing") == true
            Text(indexerText(ix, c).replaceFirstChar { it.uppercase() }, style = MaterialTheme.typography.titleMedium)
            // "Model loaded · unloads in ~1 min 40 s if nothing new" / "Not loaded · GPU memory free" (the status' `unload`)
            when {
                sv.s("status") == "error" -> Hint("Model failed to load: ${sv.str("error")}")
                sv.s("container") == "missing" -> Hint("Model server not installed yet")
                else -> UnloadLine(st.o("unload"), "Model")
            }
            val assets = c.i("assets") ?: 0
            val indexed = c.i("indexed") ?: 0
            LinearProgressIndicator(progress = { if (assets > 0) indexed.toFloat() / assets else 0f }, modifier = Modifier.fillMaxWidth())
            Text("%,d of %,d indexed".format(indexed, assets) +
                (ix.i("etaMinutes")?.takeIf { (c.i("pending") ?: 0) > 0 }?.let { " · about $it min left" } ?: ""))
            Hint("Still to do %,d · videos %,d of %,d · %,d pictures · could not index %,d · %s MB".format(
                c.i("pending") ?: 0, c.i("videosIndexed") ?: 0, c.i("videos") ?: 0, c.i("frames") ?: 0, c.i("failed") ?: 0,
                c.d("sizeMB")?.toString() ?: "0") +
                ((c.i("cleared") ?: 0).takeIf { it > 0 }?.let { " · skipped %,d (not photos or videos, or cleared)".format(it) } ?: ""))
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Button(onClick = {
                    vm.action("index", buildJsonObject { put("action", if (indexing) "pause" else "start") },
                        if (indexing) "Paused. The model unloads by itself after 20 quiet minutes." else "Indexing started.")
                }) { Text(if (indexing) "Pause" else if (indexed > 0) "Resume indexing" else "Build index") }
                OutlinedButton(
                    enabled = sv.s("container") == "running" || indexing,
                    onClick = { vm.action("unload", JsonObject(emptyMap()), "Stopped; the Search+ model is unloaded.") },
                ) { Text("Stop & free GPU") }
            }
            var frames by remember { mutableFloatStateOf((cfg.i("video_frames") ?: 4).toFloat()) }
            Text("Frames per video / GIF: ${frames.toInt()}")
            Slider(
                value = frames, onValueChange = { frames = it }, valueRange = 1f..8f, steps = 6,
                onValueChangeFinished = {
                    vm.action("settings", buildJsonObject { putJsonObject("changes") { put("video_frames", frames.toInt()) } })
                },
            )
            Hint("More frames find short scenes inside long clips; applies to videos indexed from now on.")
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text("Keep it up to date (index new uploads)", modifier = Modifier.weight(1f))
                Switch(checked = cfg.b("keep_updated") == true, onCheckedChange = { on ->
                    vm.action("settings", buildJsonObject { putJsonObject("changes") { put("keep_updated", on) } })
                })
            }
            // whole minutes, 1-60 (the panel's limits); saved with the button so a half-typed number is never sent
            val savedUnload = cfg.i("unload_after") ?: 2
            val savedCheck = cfg.i("check_every") ?: 1
            var unloadAfter by remember { mutableIntStateOf(savedUnload) }
            var checkEvery by remember { mutableIntStateOf(savedCheck) }
            NumberField("Unload the model after (minutes)", unloadAfter, { unloadAfter = it }, Modifier.fillMaxWidth(), max = 60)
            Hint("When everything is indexed and nothing new has come in for this long, the model is stopped and the graphics memory is free. After a search of yours it stays loaded for 20 minutes instead.")
            NumberField("Look for new uploads every (minutes)", checkEvery, { checkEvery = it }, Modifier.fillMaxWidth(), max = 60)
            Hint("One tiny question to Immich's database. A new photo is indexed once Immich has made its preview.")
            FilledTonalButton(
                enabled = unloadAfter != savedUnload || checkEvery != savedCheck,
                onClick = {
                    vm.action("settings", buildJsonObject {
                        putJsonObject("changes") { put("unload_after", unloadAfter); put("check_every", checkEvery) }
                    }, "Saved.")
                },
            ) { Text("Save") }
            val failures = st.a("failures").objects()
            if ((c.i("failed") ?: 0) > 0) {
                Text("Could not index ${(c.i("failed") ?: 0).plural("item")}", style = MaterialTheme.typography.titleSmall)
                failures.forEach { f ->
                    Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        AsyncImage(model = Graph.api.thumbUrl(f.str("id")), contentDescription = null, modifier = Modifier.size(40.dp))
                        Text(f.str("error"), style = MaterialTheme.typography.bodySmall)
                    }
                }
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    FilledTonalButton(onClick = { vm.action("index", buildJsonObject { put("action", "retry") }, "They will be tried again.") }) { Text("Try again") }
                    FilledTonalButton(onClick = { vm.action("index", buildJsonObject { put("action", "clear") }, "Cleared (Try again brings them back).") }) { Text("Clear list") }
                }
            }
            TextButton(onClick = {
                confirm = Confirm("Start over?", "Delete the Search+ index and start again from nothing? (Immich is not touched.)", "Delete index", danger = true) {
                    vm.action("index", buildJsonObject { put("action", "reset"); put("confirm", "reset") }, "Index deleted. Press Build index to start again.")
                }
            }) { Text("Start over…", color = MaterialTheme.colorScheme.error) }
        }
    }
    ConfirmDialog(confirm) { confirm = null }
}
