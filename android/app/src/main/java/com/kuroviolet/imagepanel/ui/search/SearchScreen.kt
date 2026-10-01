package com.kuroviolet.imagepanel.ui.search

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.grid.GridCells
import androidx.compose.foundation.lazy.grid.LazyVerticalGrid
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.ExpandLess
import androidx.compose.material.icons.filled.ExpandMore
import androidx.compose.material.icons.filled.PhotoAlbum
import androidx.compose.material3.Button
import androidx.compose.material3.ExtendedFloatingActionButton
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateListOf
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
import com.kuroviolet.imagepanel.Graph
import com.kuroviolet.imagepanel.model.Asset
import com.kuroviolet.imagepanel.model.Ops
import com.kuroviolet.imagepanel.model.Pending
import com.kuroviolet.imagepanel.model.RefData
import com.kuroviolet.imagepanel.model.PanelPrefs
import com.kuroviolet.imagepanel.model.UiBus
import com.kuroviolet.imagepanel.model.ViewerSession
import com.kuroviolet.imagepanel.net.a
import com.kuroviolet.imagepanel.net.i
import com.kuroviolet.imagepanel.net.obj
import com.kuroviolet.imagepanel.net.str
import com.kuroviolet.imagepanel.net.strings
import com.kuroviolet.imagepanel.ui.components.BusyLayer
import com.kuroviolet.imagepanel.ui.components.CheckRow
import com.kuroviolet.imagepanel.ui.components.ChipRow
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
import com.kuroviolet.imagepanel.ui.components.SuggestField
import com.kuroviolet.imagepanel.ui.components.albumOptions
import com.kuroviolet.imagepanel.ui.components.assetItems
import com.kuroviolet.imagepanel.ui.components.fullSpan
import com.kuroviolet.imagepanel.ui.components.plural
import kotlinx.coroutines.launch
import kotlinx.serialization.json.add
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import kotlinx.serialization.json.putJsonArray
import kotlinx.serialization.json.putJsonObject

class SearchVm : ViewModel() {
    var mode by mutableStateOf("query")
    val engine: String get() = PanelPrefs.searchEngine

    /** Immich's smart search or the Search+ model; remembered (shared with the web panel). */
    fun chooseEngine(e: String) {
        if (e == PanelPrefs.searchEngine) return
        PanelPrefs.searchEngine = e
        viewModelScope.launch { PanelPrefs.save("searchEngine", e) }
    }
    var query by mutableStateOf("")
    var like by mutableStateOf("")

    var personInput by mutableStateOf("")
    val people = mutableStateListOf<String>()
    var peopleMatch by mutableStateOf("all")

    var limit by mutableIntStateOf(200)
    var type by mutableStateOf("")
    var after by mutableStateOf("")
    var before by mutableStateOf("")
    var skipInput by mutableStateOf("")
    val skipAlbums = mutableStateListOf<String>()
    var unfiled by mutableStateOf(false)
    var allOf by mutableStateOf("")
    var noneOf by mutableStateOf("")
    var showFilters by mutableStateOf(false)

    var results by mutableStateOf<List<Asset>>(emptyList())
    val selection = Selection()
    var summary by mutableStateOf<String?>(null)
    val sessionSkip = mutableStateListOf<String>()
    var busy by mutableStateOf<String?>(null)

    fun addPerson(typed: String) {
        val t = typed.trim()
        if (t.isEmpty()) return
        val person = RefData.people.firstOrNull { it.label == t } ?: RefData.people.firstOrNull { it.name.equals(t, true) }
        if (person == null) {
            UiBus.error("No named person “$t”. Pick one from the list (only named people can be searched).")
            return
        }
        if (person.id !in people) people += person.id
        personInput = ""
    }

    fun addSkip(name: String) {
        val n = name.trim()
        if (n.isEmpty()) return
        if (RefData.albums.none { it.name == n }) {
            UiBus.error("No album called “$n”. Pick one from the list.")
            return
        }
        if (n !in skipAlbums) skipAlbums += n
        skipInput = ""
    }

    private fun terms(s: String) = s.split(',').map { it.trim() }.filter { it.isNotEmpty() }

    fun run() {
        if (mode == "query" && query.isBlank() && people.isEmpty()) return UiBus.error("Describe what you are looking for, or pick people.")
        if (mode == "like" && like.isBlank()) return UiBus.error("Paste an asset ID to match against.")
        val body = buildJsonObject {
            put("limit", limit)
            put("engine", engine)
            putJsonObject("filters") {
                if (type.isNotEmpty()) put("type", type)
                if (after.isNotEmpty()) put("taken_after", after)
                if (before.isNotEmpty()) put("taken_before", before)
                if (unfiled) put("only_unfiled", true)
            }
            putJsonObject("refine") {
                putJsonArray("all_of") { terms(allOf).forEach { add(it) } }
                putJsonArray("none_of") { terms(noneOf).forEach { add(it) } }
            }
            putJsonArray("excludeAlbums") { skipAlbums.forEach { add(it) } }
            putJsonArray("people") { people.forEach { add(it) } }
            put("peopleMatch", peopleMatch)
            putJsonArray("skipIds") { sessionSkip.forEach { add(it) } }
            when (mode) {
                "query" -> put("query", query.trim())
                else -> put("like", like.trim())
            }
        }
        busy = if (engine == "searchplus" && mode == "query" && query.isNotBlank()) "Searching with the Search+ model… (the first search loads it: up to ~30 s)"
        else if (people.size > 1 && peopleMatch == "any") "Searching for photos with any of these people… (up to ~15 s)"
        else "Searching your library for up to %,d…".format(limit)
        viewModelScope.launch {
            try {
                val data = Graph.api.post("/api/search", body).obj()
                results = Asset.list(data.a("assets"))
                selection.selectAll(results.map { it.id })   // everything starts ticked, like the panel
                var msg = if (results.isEmpty()) "No matches" else "Found ${results.size.plural("item")}"
                val total = data.i("total") ?: 0
                if (total > results.size) msg += " (of %,d — raise Max results for more)".format(total)
                val excluded = data.i("excluded") ?: 0
                if (excluded > 0) msg += " · skipped %,d from your skip list".format(excluded)
                summary = data.str("match").ifEmpty { null }
                UiBus.toast("$msg.")
                val unknown = data.a("unknownAlbums").strings()
                if (unknown.isNotEmpty()) UiBus.error("No album named ${unknown.joinToString()} — nothing skipped for it.")
            } catch (e: Exception) {
                UiBus.error(e, "search")
            } finally {
                busy = null
            }
        }
    }

    /** "Save as smart album…": hands this search to the smart album editor (as "the best N", not hourly yet). */
    fun draftSmartAlbum(): Boolean {
        if (mode == "query" && query.isBlank() && people.isEmpty() || mode == "like" && like.isBlank()) {
            UiBus.error("Set up a search first.")
            return false
        }
        val who = people.map { RefData.personName(it) }
        val name = (if (mode == "query") query.trim() else "").ifEmpty { who.joinToString(" & ") }.ifEmpty { "Similar photos" }.take(40)
        ThemeDraft.prefill = buildJsonObject {
            put("source", if (mode == "like") "like" else if (query.isNotBlank()) "text" else "none")
            put("engine", engine)
            put("cutoff", if (engine == "searchplus") 0.17 else 0.1)
            put("description", if (mode == "query") query.trim() else "")
            put("like", if (mode == "like") like.trim() else "")
            put("name", name); put("album", name)
            put("mode", "top"); put("limit", limit); put("enabled", false)
            put("media", type)
            putJsonArray("people") { people.forEach { add(it) } }
            put("people_match", peopleMatch)
            put("taken_after", after); put("taken_before", before)
            putJsonArray("exclude_albums") { skipAlbums.forEach { add(it) } }
            put("only_unfiled", unfiled)
            putJsonArray("all_of") { terms(allOf).forEach { add(it) } }
            putJsonArray("none_of") { terms(noneOf).forEach { add(it) } }
        }
        UiBus.toast("Filled in from your search as “the best N”. Preview, adjust, then Save.")
        return true
    }

    fun file(req: FileRequest) {
        val ids = selection.ids(results)
        busy = "${if (req.move) "Moving" else "Adding"} ${ids.size} to ${req.album}…"
        viewModelScope.launch {
            try {
                UiBus.toast(Ops.file(ids, req))
                val filed = ids.toSet()
                if (req.skipNext) sessionSkip.addAll(filed.filter { it !in sessionSkip })
                results = results.filter { it.id !in filed }
                filed.forEach { selection.set(it, false) }
            } catch (e: Exception) {
                UiBus.error(e, "file")
            } finally {
                busy = null
            }
        }
    }
}

@Composable
fun SearchScreen(nav: NavController) {
    val vm: SearchVm = viewModel()
    var fileSheet by remember { mutableStateOf(false) }
    LaunchedEffect(Pending.searchLike) {
        Pending.searchLike?.let { id ->
            Pending.searchLike = null
            vm.mode = "like"
            vm.like = id
            vm.run()
        }
    }
    Scaffold(
        topBar = { ScreenTop("Search") },
        floatingActionButton = {
            if (vm.selection.size > 0 && vm.results.isNotEmpty()) {
                ExtendedFloatingActionButton(
                    onClick = { fileSheet = true },
                    icon = { Icon(Icons.Filled.PhotoAlbum, contentDescription = null) },
                    text = { Text("Album… (${vm.selection.size})") },
                )
            }
        },
    ) { padding ->
        Box(Modifier.fillMaxSize().padding(padding)) {
            LazyVerticalGrid(
                columns = GridCells.Adaptive(Graph.settings.tileDp.dp),
                contentPadding = PaddingValues(start = 10.dp, end = 10.dp, top = 4.dp, bottom = 96.dp),
                horizontalArrangement = Arrangement.spacedBy(4.dp),
                verticalArrangement = Arrangement.spacedBy(4.dp),
                modifier = Modifier.fillMaxSize(),
            ) {
                fullSpan("form") { SearchForm(vm) { if (vm.draftSmartAlbum()) nav.navigate("theme/new") } }
                if (vm.results.isNotEmpty() || vm.summary != null) {
                    fullSpan("head") {
                        Row(Modifier.fillMaxWidth().padding(top = 10.dp, bottom = 4.dp), verticalAlignment = Alignment.CenterVertically) {
                            Column(Modifier.weight(1f)) {
                                Text("${vm.results.size.plural("result")} · ${vm.selection.size} selected", style = MaterialTheme.typography.titleSmall)
                                vm.summary?.let { Hint(it) }
                            }
                            TextButton(onClick = { vm.selection.selectAll(vm.results.map { it.id }) }) { Text("All") }
                            TextButton(onClick = { vm.selection.clear() }) { Text("None") }
                        }
                    }
                    fullSpan("hint") { Hint("Tap a photo to open it; the circle ticks it. Smart search has no score, so the tail of a long list is usually noise.") }
                }
                assetItems(vm.results, vm.selection) { index ->
                    ViewerSession.open(vm.results, index, vm.selection, "Search")
                    nav.navigate("viewer")
                }
            }
            BusyLayer(vm.busy)
        }
    }
    if (fileSheet) {
        FileSheet(count = vm.selection.size, onClose = { fileSheet = false }, onConfirm = { vm.file(it) })
    }
}

@Composable
private fun SearchForm(vm: SearchVm, onSaveAsSmart: () -> Unit) {
    val focus = LocalFocusManager.current
    SectionCard {
        Segmented(listOf("query" to "Describe it", "like" to "Like this"), vm.mode, { vm.mode = it })
        Segmented(listOf("immich" to "Immich model", "searchplus" to "Search+ model"), vm.engine, { vm.chooseEngine(it) })
        if (vm.engine == "searchplus") {
            Hint("Scores with the Search+ index (the bigger PE-Core model), with all the filters below. Only photos already " +
                "in the Search+ index are found; “must also / must not match” words count from a score of 0.15.")
        }
        when (vm.mode) {
            "query" -> OutlinedTextField(
                value = vm.query, onValueChange = { vm.query = it },
                label = { Text("What are you looking for? (optional if you pick people)") },
                placeholder = { Text("person in a mountain") },
                singleLine = true, modifier = Modifier.fillMaxWidth(),
                keyboardOptions = KeyboardOptions(imeAction = ImeAction.Search),
                keyboardActions = KeyboardActions(onSearch = { focus.clearFocus(); vm.run() }),
            )
            else -> {
                OutlinedTextField(
                    value = vm.like, onValueChange = { vm.like = it.trim() },
                    label = { Text("Reference asset ID") }, singleLine = true, modifier = Modifier.fillMaxWidth(),
                )
                Hint("Or open any photo and choose “Like this (Search tab)” from its menu: it uses the model picked here.")
            }
        }

        // People
        SuggestField(
            value = vm.personInput, onValueChange = { vm.personInput = it },
            label = "People in the photo",
            options = RefData.people.map { it.label to "" },
            onPick = { vm.addPerson(it) }, onDone = { vm.addPerson(vm.personInput) },
        )
        ChipRow(vm.people, onRemove = { vm.people.remove(it) }, label = { RefData.personName(it) })
        if (vm.people.size >= 2) {
            Segmented(listOf("all" to "All of them together", "any" to "Any of them"), vm.peopleMatch, { vm.peopleMatch = it })
        }
        Hint("${RefData.people.size} named people. Pick several to find photos where they all appear together." +
            if (RefData.unnamedPeople > 0) " ${RefData.unnamedPeople} unnamed can't be picked until you name them in Immich." else "")

        TextButton(onClick = { vm.showFilters = !vm.showFilters }) {
            Icon(if (vm.showFilters) Icons.Filled.ExpandLess else Icons.Filled.ExpandMore, contentDescription = null)
            Spacer(Modifier.padding(start = 4.dp))
            Text("Filters & refinement")
        }
        if (vm.showFilters) Filters(vm)
        Button(onClick = { focus.clearFocus(); vm.run() }, modifier = Modifier.fillMaxWidth()) { Text("Search") }
        TextButton(onClick = onSaveAsSmart, modifier = Modifier.fillMaxWidth()) { Text("Save as smart album…") }
    }
}

@Composable
private fun Filters(vm: SearchVm) {
    Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            NumberField("Max results", vm.limit, { vm.limit = it }, Modifier.weight(1f), max = 10000)
        }
        Hint("Up to 10,000. Best matches come first.")
        Segmented(MEDIA_OPTIONS, vm.type, { vm.type = it })
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            DateField("Taken after", vm.after, { vm.after = it }, Modifier.weight(1f))
            DateField("Taken before", vm.before, { vm.before = it }, Modifier.weight(1f))
        }
        SuggestField(
            value = vm.skipInput, onValueChange = { vm.skipInput = it },
            label = "Skip photos that are in these albums",
            options = albumOptions(RefData.albums), onPick = { vm.addSkip(it) }, onDone = { vm.addSkip(vm.skipInput) },
        )
        ChipRow(vm.skipAlbums, onRemove = { vm.skipAlbums.remove(it) })
        if (vm.sessionSkip.isNotEmpty()) {
            ChipRow(listOf("session"), onRemove = { vm.sessionSkip.clear() }, label = { "${vm.sessionSkip.size.plural("photo")} filed this session" })
        }
        Hint("Skipped photos don't count toward Max results — the search keeps going, so you still get new ones.")
        CheckRow("Only photos that are in no album at all", vm.unfiled) { vm.unfiled = it }
        OutlinedTextField(
            value = vm.allOf, onValueChange = { vm.allOf = it },
            label = { Text("Must also match (comma separated)") }, placeholder = { Text("snow, hiking boots") },
            singleLine = true, modifier = Modifier.fillMaxWidth(),
        )
        OutlinedTextField(
            value = vm.noneOf, onValueChange = { vm.noneOf = it },
            label = { Text("Must not match") }, placeholder = { Text("ski resort") },
            singleLine = true, modifier = Modifier.fillMaxWidth(),
        )
    }
}
