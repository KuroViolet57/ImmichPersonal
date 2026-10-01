package com.kuroviolet.imagepanel.ui.more

import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.lazy.grid.GridCells
import androidx.compose.foundation.lazy.grid.LazyVerticalGrid
import androidx.compose.foundation.lazy.grid.itemsIndexed
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.ExpandLess
import androidx.compose.material.icons.filled.ExpandMore
import androidx.compose.material.icons.filled.OpenInFull
import androidx.compose.material3.Button
import androidx.compose.material3.FilledTonalButton
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Surface
import androidx.compose.material3.Switch
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
import androidx.compose.ui.draw.alpha
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalFocusManager
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import androidx.lifecycle.viewmodel.compose.viewModel
import androidx.navigation.NavController
import coil3.compose.AsyncImage
import com.kuroviolet.imagepanel.Graph
import com.kuroviolet.imagepanel.model.Asset
import com.kuroviolet.imagepanel.model.RefData
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
import com.kuroviolet.imagepanel.net.strings
import com.kuroviolet.imagepanel.ui.albums.AlbumStore
import com.kuroviolet.imagepanel.ui.components.BusyLayer
import com.kuroviolet.imagepanel.ui.components.CheckRow
import com.kuroviolet.imagepanel.ui.components.ChipRow
import com.kuroviolet.imagepanel.ui.components.Confirm
import com.kuroviolet.imagepanel.ui.components.ConfirmDialog
import com.kuroviolet.imagepanel.ui.components.DateField
import com.kuroviolet.imagepanel.ui.components.Hint
import com.kuroviolet.imagepanel.ui.components.MEDIA_OPTIONS
import com.kuroviolet.imagepanel.ui.components.NumberField
import com.kuroviolet.imagepanel.ui.components.ScreenTop
import com.kuroviolet.imagepanel.ui.components.SectionCard
import com.kuroviolet.imagepanel.ui.components.Segmented
import com.kuroviolet.imagepanel.ui.components.SuggestField
import com.kuroviolet.imagepanel.ui.components.albumOptions
import com.kuroviolet.imagepanel.ui.components.fullSpan
import kotlinx.coroutines.launch
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.add
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import kotlinx.serialization.json.putJsonArray
import java.util.Locale
import kotlin.math.floor

/** A smart album handed over from another screen ("Save as smart album" in Search). */
object ThemeDraft {
    var prefill: JsonObject? = null
}

class SmartAlbumEditorVm : ViewModel() {
    var editingId by mutableStateOf<String?>(null)
    var editingName by mutableStateOf("")
    private var started = false

    var source by mutableStateOf("text")
    var engine by mutableStateOf("immich")
    var description by mutableStateOf("")
    var like by mutableStateOf("")
    var name by mutableStateOf("")
    var album by mutableStateOf("")
    var mode by mutableStateOf("cutoff")
    var cutoff by mutableStateOf("0.100")
    var limit by mutableIntStateOf(200)
    var enabled by mutableStateOf(true)
    var media by mutableStateOf("")
    var personInput by mutableStateOf("")
    val people = mutableStateListOf<String>()
    var peopleMatch by mutableStateOf("all")
    var after by mutableStateOf("")
    var before by mutableStateOf("")
    var skipInput by mutableStateOf("")
    val skipAlbums = mutableStateListOf<String>()
    var unfiled by mutableStateOf(false)
    var allOf by mutableStateOf("")
    var noneOf by mutableStateOf("")
    var archive by mutableStateOf(false)
    var favorite by mutableStateOf(false)
    var showFilters by mutableStateOf(false)

    var preview by mutableStateOf<List<Asset>>(emptyList())
    var previewCounts by mutableStateOf<JsonObject?>(null)
    var previewScored by mutableStateOf(true)
    var busy by mutableStateOf<String?>(null)

    fun start(id: String) {
        if (started) return
        started = true
        if (id == "new") {
            ThemeDraft.prefill?.let { fill(it) }
            ThemeDraft.prefill = null
            return
        }
        busy = "Loading…"
        viewModelScope.launch {
            try {
                val theme = Graph.api.get("/api/themes").obj().a("themes").objects().firstOrNull { it.str("id") == id }
                if (theme == null) UiBus.error("That smart album no longer exists.") else {
                    editingId = id
                    editingName = theme.str("name")
                    fill(theme)
                }
            } catch (e: Exception) { UiBus.error(e, "smart album") } finally { busy = null }
        }
    }

    private fun fill(t: JsonObject) {
        source = t.s("source") ?: "text"
        engine = t.s("engine") ?: "immich"
        description = t.str("description")
        like = t.str("like")
        name = t.str("name")
        album = t.str("album")
        mode = t.s("mode") ?: "cutoff"
        cutoff = String.format(Locale.US, "%.3f", t.d("cutoff") ?: 0.1)
        limit = (t.i("limit") ?: 200).coerceIn(1, 10000)
        enabled = t.b("enabled") ?: true
        media = t.str("media")
        people.clear(); people.addAll(t.a("people").strings())
        peopleMatch = t.s("people_match") ?: "all"
        after = t.str("taken_after").take(10)
        before = t.str("taken_before").take(10)
        skipAlbums.clear(); skipAlbums.addAll(t.a("exclude_albums").strings())
        unfiled = t.b("only_unfiled") == true
        allOf = t.a("all_of").strings().joinToString(", ")
        noneOf = t.a("none_of").strings().joinToString(", ")
        archive = t.b("archive") == true
        favorite = t.b("favorite") == true
        showFilters = people.isNotEmpty() || media.isNotEmpty() || after.isNotEmpty() || before.isNotEmpty() ||
            skipAlbums.isNotEmpty() || unfiled || allOf.isNotEmpty() || noneOf.isNotEmpty() || archive || favorite
    }

    /** Switching model also moves an untouched default cut-off to the new model's scale. */
    fun switchEngine(to: String) {
        if (to == engine) return
        val defaults = mapOf("immich" to 0.1, "searchplus" to 0.17)
        if (source == "text" && cutoffValue == defaults[engine]) cutoff = String.format(Locale.US, "%.3f", defaults[to])
        engine = to
        preview = emptyList(); previewCounts = null
    }

    fun addPerson(typed: String) {
        val t = typed.trim()
        if (t.isEmpty()) return
        val person = RefData.people.firstOrNull { it.label == t } ?: RefData.people.firstOrNull { it.name.equals(t, true) }
        if (person == null) return UiBus.error("No named person “$t”. Pick one from the list.")
        if (person.id !in people) people += person.id
        personInput = ""
    }

    fun addSkip(typed: String) {
        val n = typed.trim()
        if (n.isEmpty()) return
        if (RefData.albums.none { it.name == n }) return UiBus.error("No album called “$n”. Pick one from the list.")
        if (n !in skipAlbums) skipAlbums += n
        skipInput = ""
    }

    val cutoffValue: Double? get() = cutoff.replace(',', '.').toDoubleOrNull()?.takeIf { it > 0 && it < 1 }

    private fun terms(s: String) = s.split(',').map { it.trim() }.filter { it.isNotEmpty() }

    private fun spec(): JsonObject {
        val desc = description.trim()
        val label = desc.ifEmpty { if (like.isNotBlank()) "Similar photos" else "People" }
        return buildJsonObject {
            editingId?.let { put("id", it) }
            put("source", source)
            put("engine", engine)
            put("description", desc)
            put("like", like.trim().ifEmpty { null })
            put("name", name.trim().ifEmpty { label })
            put("album", album.trim().ifEmpty { name.trim().ifEmpty { label } })
            put("mode", mode)
            put("cutoff", cutoffValue ?: 0.1)
            put("limit", limit)
            put("enabled", enabled)
            put("media", media)
            putJsonArray("people") { people.forEach { add(it) } }
            put("people_match", peopleMatch)
            put("taken_after", after.ifEmpty { null })
            put("taken_before", before.ifEmpty { null })
            putJsonArray("exclude_albums") { skipAlbums.forEach { add(it) } }
            put("only_unfiled", unfiled)
            putJsonArray("all_of") { terms(allOf).forEach { add(it) } }
            putJsonArray("none_of") { terms(noneOf).forEach { add(it) } }
            put("archive", archive)
            put("favorite", favorite)
        }
    }

    private fun checkSource(): Boolean {
        when {
            source == "text" && description.isBlank() -> UiBus.error("Describe what the photos should show first.")
            source == "like" && like.isBlank() -> UiBus.error("Paste the ID of the photo to match.")
            source == "none" && people.isEmpty() -> UiBus.error("Pick at least one person under Filters.")
            mode == "cutoff" && cutoffValue == null -> UiBus.error("The cut-off must be a number between 0 and 1, like 0.100.")
            else -> return true
        }
        return false
    }

    fun runPreview() {
        if (!checkSource()) return
        busy = "Scoring your whole library…"
        viewModelScope.launch {
            try {
                val data = Graph.api.post("/api/themes/preview", buildJsonObject {
                    put("theme", spec()); put("limit", 300)
                }).obj()
                // type unknown here: the viewer looks it up, so videos still play
                preview = data.a("items").objects().mapNotNull { o ->
                    o.s("id")?.let { Asset(id = it, type = "", score = o.d("score")) }
                }
                previewCounts = data.o("counts")
                previewScored = data.b("scored") != false
            } catch (e: Exception) { UiBus.error(e, "preview") } finally { busy = null }
        }
    }

    /** Tapping a preview photo moves the line to it: everything up to here belongs. */
    fun cutAt(index: Int) {
        if (mode == "top") limit = index + 1
        else preview.getOrNull(index)?.score?.let { cutoff = String.format(Locale.US, "%.3f", floor(it * 1000) / 1000) }
    }

    fun inside(index: Int): Boolean {
        val a = preview[index]
        return if (mode == "top") index < limit else (a.score == null || a.score >= (cutoffValue ?: 1.0))
    }

    fun previewText(): String {
        val all = preview.size
        val counts = previewCounts ?: return ""
        val total = counts.i("total") ?: all
        if (mode == "top") return "Adds the best %,d of %,d photos that pass the filters (the preview shows up to %d).".format(limit, total, all)
        if (!previewScored) return "%,d photos match the filters (the preview shows up to %d).".format(total, all)
        val c = cutoffValue ?: return ""
        val inside = preview.indices.count { inside(it) }
        val buckets = counts.keys.filter { it != "total" }.joinToString(" · ") { k -> "$k: %,d".format(counts.i(k) ?: 0) }
        val head = if (inside == all && all > 0) "At %.3f, at least %d photos match (only the best %d are shown). ".format(Locale.US, c, all, all)
        else "At %.3f, %,d photos match. ".format(Locale.US, c, inside)
        return head + if (buckets.isNotEmpty()) "Counts per cut-off — $buckets." else ""
    }

    fun save(onSaved: () -> Unit) {
        if (!checkSource()) return
        busy = "Saving…"
        viewModelScope.launch {
            try {
                val data = Graph.api.post("/api/themes/save", buildJsonObject { put("theme", spec()) }).obj()
                val t = data.o("theme")
                editingId = t.str("id")
                editingName = t.str("name")
                UiBus.toast("Saved “${t.str("name")}”. Run it now to fill the album straight away.")
                onSaved()
            } catch (e: Exception) { UiBus.error(e, "save smart album") } finally { busy = null }
        }
    }

    fun runNow() {
        val id = editingId ?: return
        busy = "Running smart album…"
        viewModelScope.launch {
            try {
                val data = Graph.api.post("/api/themes/run", buildJsonObject { putJsonArray("ids") { add(id) } }).obj()
                val r = data.a("results").objects().firstOrNull()
                if (r?.s("error") != null) UiBus.error("Run failed: ${r.str("error")}")
                else UiBus.toast("Added ${r?.i("added") ?: 0} photo(s) to “${album.ifEmpty { name }}”.")
                RefData.loadAlbums()
                runCatching { AlbumStore.load() }
            } catch (e: Exception) { UiBus.error(e, "run smart album") } finally { busy = null }
        }
    }

    fun forget() {
        val id = editingId ?: return
        viewModelScope.launch {
            try {
                Graph.api.post("/api/themes/forget", buildJsonObject { put("id", id) })
                UiBus.toast("Done — removed photos may be added again on the next run.")
            } catch (e: Exception) { UiBus.error(e, "smart album") }
        }
    }
}

@Composable
fun SmartAlbumEditorScreen(nav: NavController, id: String) {
    val vm: SmartAlbumEditorVm = viewModel(key = "theme-$id")
    LaunchedEffect(id) { vm.start(id) }
    var confirm by remember { mutableStateOf<Confirm?>(null) }
    val title = when {
        vm.editingId != null -> "Edit “${vm.editingName}”"
        else -> "New smart album"
    }
    Scaffold(topBar = { ScreenTop(title, nav) }) { padding ->
        Box(Modifier.fillMaxSize().padding(padding).imePadding()) {
            LazyVerticalGrid(
                columns = GridCells.Adaptive(Graph.settings.tileDp.dp),
                contentPadding = PaddingValues(start = 10.dp, end = 10.dp, top = 4.dp, bottom = 40.dp),
                horizontalArrangement = Arrangement.spacedBy(4.dp),
                verticalArrangement = Arrangement.spacedBy(4.dp),
                modifier = Modifier.fillMaxSize(),
            ) {
                fullSpan("form") { EditorForm(vm, onSaved = {}, onForget = {
                    confirm = Confirm("Allow re-adding?", "Let this smart album add back photos you removed from its album " +
                        "(or undid) on its next run?", "Allow") { vm.forget() }
                }) }
                if (vm.preview.isNotEmpty()) {
                    fullSpan("count") { Hint(vm.previewText(), Modifier.padding(vertical = 6.dp)) }
                    fullSpan("how") {
                        Hint("Tap a photo to move the line to it (everything up to it belongs). The ⤢ button opens it.")
                    }
                    itemsIndexed(vm.preview, key = { _, a -> a.id }) { index, asset ->
                        PreviewTile(
                            asset = asset, index = index, inside = vm.inside(index),
                            onCut = { vm.cutAt(index) },
                            onOpen = {
                                ViewerSession.open(vm.preview, index, null, "Preview")
                                nav.navigate("viewer")
                            },
                        )
                    }
                } else if (vm.previewCounts != null) {
                    fullSpan("none") { Hint("Nothing matches yet — loosen the cut-off or the filters.", Modifier.padding(8.dp)) }
                }
            }
            BusyLayer(vm.busy)
        }
    }
    ConfirmDialog(confirm) { confirm = null }
}

@Composable
private fun EditorForm(vm: SmartAlbumEditorVm, onSaved: () -> Unit, onForget: () -> Unit) {
    val focus = LocalFocusManager.current
    SectionCard {
        Text("Model that scores the photos", style = MaterialTheme.typography.labelLarge)
        Segmented(listOf("immich" to "Immich", "searchplus" to "Search+ (PE-Core)"), vm.engine, { vm.switchEngine(it) })
        Hint(if (vm.engine == "searchplus") "The bigger Search+ model. Its scores sit higher (around 0.17 is a typical cut-off), " +
            "so preview first. Only photos already in the Search+ index can be added."
            else "Immich's own smart-search model, the same one as the Search tab.")
        Segmented(listOf("text" to "Describe it", "like" to "Like a photo", "none" to "Only people"), vm.source, { vm.source = it })
        when (vm.source) {
            "text" -> OutlinedTextField(
                value = vm.description, onValueChange = { vm.description = it },
                label = { Text("Description (what the photos are compared with)") },
                placeholder = { Text("Five Nights at Freddy's") },
                singleLine = true, modifier = Modifier.fillMaxWidth(),
            )
            "like" -> {
                Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    if (vm.like.trim().length == 36) {
                        AsyncImage(model = Graph.api.thumbUrl(vm.like.trim()), contentDescription = null,
                            contentScale = ContentScale.Crop, modifier = Modifier.size(56.dp).clip(RoundedCornerShape(6.dp)))
                    }
                    OutlinedTextField(
                        value = vm.like, onValueChange = { vm.like = it.trim() },
                        label = { Text("The photo to match (asset ID)") }, singleLine = true, modifier = Modifier.weight(1f),
                    )
                }
                Hint("Scores here are photo-to-photo, so good cut-offs are much higher (often 0.6–0.9): use the preview.")
            }
            else -> Hint("Collects every photo of the people you pick under Filters.")
        }
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            OutlinedTextField(
                value = vm.name, onValueChange = { vm.name = it }, label = { Text("Name") },
                placeholder = { Text("same as the description") }, singleLine = true, modifier = Modifier.weight(1f),
            )
        }
        SuggestField(
            value = vm.album, onValueChange = { vm.album = it }, label = "Album it fills",
            placeholder = "same as the name", options = albumOptions(RefData.albums), onPick = { vm.album = it },
        )

        Text("Which matches to add", style = MaterialTheme.typography.labelLarge)
        Segmented(listOf("cutoff" to "Above a score", "top" to "The best N"), vm.mode, { vm.mode = it })
        if (vm.mode == "cutoff") {
            OutlinedTextField(
                value = vm.cutoff, onValueChange = { t -> vm.cutoff = t.filter { it.isDigit() || it == '.' || it == ',' }.take(6) },
                label = { Text("Cut-off score") }, singleLine = true, isError = vm.cutoffValue == null,
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Decimal, imeAction = ImeAction.Done),
                supportingText = { Text("Higher = stricter. Easiest: preview, then tap the last photo that still belongs.") },
                modifier = Modifier.fillMaxWidth(),
            )
        } else {
            NumberField("How many (best first)", vm.limit, { vm.limit = it }, Modifier.fillMaxWidth(), max = 10000)
        }
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text("Run every hour", modifier = Modifier.weight(1f))
            Switch(checked = vm.enabled, onCheckedChange = { vm.enabled = it })
        }

        TextButton(onClick = { vm.showFilters = !vm.showFilters }) {
            Icon(if (vm.showFilters) Icons.Filled.ExpandLess else Icons.Filled.ExpandMore, contentDescription = null)
            Spacer(Modifier.padding(start = 4.dp))
            Text("Filters")
        }
        if (vm.showFilters) EditorFilters(vm)

        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            FilledTonalButton(onClick = { focus.clearFocus(); vm.runPreview() }, modifier = Modifier.weight(1f)) { Text("Preview matches") }
            Button(onClick = { focus.clearFocus(); vm.save(onSaved) }, modifier = Modifier.weight(1f)) { Text("Save") }
        }
        if (vm.editingId != null) {
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                TextButton(onClick = { vm.runNow() }) { Text("Run now") }
                TextButton(onClick = onForget) { Text("Allow re-adding removed photos") }
            }
        }
        Hint("A smart album only ever adds: nothing is moved or removed, a photo you take out is never put back, " +
            "and every run can be undone from History.")
    }
}

@Composable
private fun EditorFilters(vm: SmartAlbumEditorVm) {
    Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
        Text("What it may add", style = MaterialTheme.typography.labelLarge)
        Segmented(MEDIA_OPTIONS, vm.media, { vm.media = it })
        SuggestField(
            value = vm.personInput, onValueChange = { vm.personInput = it }, label = "People in the photo",
            options = RefData.people.map { it.label to "" }, onPick = { vm.addPerson(it) }, onDone = { vm.addPerson(vm.personInput) },
        )
        ChipRow(vm.people, onRemove = { vm.people.remove(it) }, label = { RefData.personName(it) })
        if (vm.people.size >= 2) {
            Segmented(listOf("all" to "All of them together", "any" to "Any of them"), vm.peopleMatch, { vm.peopleMatch = it })
        }
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            DateField("Taken after", vm.after, { vm.after = it }, Modifier.weight(1f))
            DateField("Taken before", vm.before, { vm.before = it }, Modifier.weight(1f))
        }
        SuggestField(
            value = vm.skipInput, onValueChange = { vm.skipInput = it }, label = "Skip photos that are in these albums",
            options = albumOptions(RefData.albums), onPick = { vm.addSkip(it) }, onDone = { vm.addSkip(vm.skipInput) },
        )
        ChipRow(vm.skipAlbums, onRemove = { vm.skipAlbums.remove(it) })
        CheckRow("Only photos that are in no album at all", vm.unfiled) { vm.unfiled = it }
        OutlinedTextField(
            value = vm.allOf, onValueChange = { vm.allOf = it }, label = { Text("Must also match (comma separated)") },
            placeholder = { Text("snow, hiking boots") }, singleLine = true, modifier = Modifier.fillMaxWidth(),
        )
        OutlinedTextField(
            value = vm.noneOf, onValueChange = { vm.noneOf = it }, label = { Text("Must not match") },
            placeholder = { Text("ski resort") }, singleLine = true, modifier = Modifier.fillMaxWidth(),
        )
        Hint("Extra words are checked with the same cut-off as the description (0.10 next to “like a photo”).")
        Text("When it adds photos, also…", style = MaterialTheme.typography.labelLarge)
        CheckRow("Archive them (hide from the main timeline)", vm.archive) { vm.archive = it }
        CheckRow("Mark them as favourites", vm.favorite) { vm.favorite = it }
    }
}

@Composable
private fun PreviewTile(asset: Asset, index: Int, inside: Boolean, onCut: () -> Unit, onOpen: () -> Unit) {
    Box(
        Modifier.aspectRatio(1f).clip(RoundedCornerShape(8.dp)).background(MaterialTheme.colorScheme.surfaceVariant)
            .clickable(onClick = onCut),
    ) {
        AsyncImage(
            model = Graph.api.thumbUrl(asset.id), contentDescription = null, contentScale = ContentScale.Crop,
            modifier = Modifier.fillMaxSize().alpha(if (inside) 1f else 0.3f),
        )
        Box(Modifier.align(Alignment.TopEnd).size(40.dp).clickable(onClick = onOpen), contentAlignment = Alignment.Center) {
            Surface(shape = CircleShape, color = Color.Black.copy(alpha = 0.45f)) {
                Icon(Icons.Filled.OpenInFull, contentDescription = "Open", tint = Color.White, modifier = Modifier.padding(5.dp).size(16.dp))
            }
        }
        Text(
            "#${index + 1}" + (asset.score?.let { " · %.3f".format(Locale.US, it) } ?: ""),
            color = Color.White, fontSize = 10.sp, fontWeight = FontWeight.Medium,
            modifier = Modifier.align(Alignment.BottomStart).padding(4.dp)
                .background(Color.Black.copy(alpha = 0.6f), RoundedCornerShape(4.dp)).padding(horizontal = 4.dp, vertical = 1.dp),
        )
        if (inside) {
            Box(Modifier.align(Alignment.BottomEnd).padding(4.dp).size(10.dp).clip(CircleShape).background(MaterialTheme.colorScheme.primary))
        }
    }
}
