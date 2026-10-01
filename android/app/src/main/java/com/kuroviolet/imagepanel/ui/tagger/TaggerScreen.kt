@file:OptIn(ExperimentalMaterial3Api::class, ExperimentalLayoutApi::class)

package com.kuroviolet.imagepanel.ui.tagger

import android.graphics.BitmapFactory
import android.util.Base64
import androidx.compose.foundation.Image
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.lazy.grid.GridCells
import androidx.compose.foundation.lazy.grid.LazyVerticalGrid
import androidx.compose.foundation.rememberScrollState
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Delete
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FilledTonalButton
import androidx.compose.material3.FilterChip
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.PrimaryTabRow
import androidx.compose.material3.RadioButton
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Slider
import androidx.compose.material3.SuggestionChip
import androidx.compose.material3.Switch
import androidx.compose.material3.Tab
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import androidx.lifecycle.viewmodel.compose.viewModel
import androidx.navigation.NavController
import coil3.compose.AsyncImage
import com.kuroviolet.imagepanel.Graph
import com.kuroviolet.imagepanel.model.Asset
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
import com.kuroviolet.imagepanel.ui.components.BusyLayer
import com.kuroviolet.imagepanel.ui.components.CheckRow
import com.kuroviolet.imagepanel.ui.components.Confirm
import com.kuroviolet.imagepanel.ui.components.ConfirmDialog
import com.kuroviolet.imagepanel.ui.components.Hint
import com.kuroviolet.imagepanel.ui.components.NumberField
import com.kuroviolet.imagepanel.ui.components.ScreenTop
import com.kuroviolet.imagepanel.ui.components.SectionCard
import com.kuroviolet.imagepanel.ui.components.Segmented
import com.kuroviolet.imagepanel.ui.components.Selection
import com.kuroviolet.imagepanel.ui.components.assetItems
import com.kuroviolet.imagepanel.ui.components.fullSpan
import com.kuroviolet.imagepanel.ui.components.plural
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.add
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import kotlinx.serialization.json.putJsonArray
import java.net.URLEncoder

/** One rule as the user edits it: comma-separated tag lists. */
data class RuleDraft(val ifAll: String = "", val ifAny: String = "", val unless: String = "", val add: String = "", val remove: String = "")

/** The "How to tag" settings being edited, kept apart from the server's copy until saved. */
data class TagDraft(
    val instructions: String = "", val vocabulary: String = "", val blocked: String = "", val language: String = "English",
    val describe: Boolean = true, val characterTags: Boolean = true, val ratingTag: Boolean = true, val writeTags: Boolean = false,
    val useWd: Boolean = true, val usePixai: Boolean = true,
    val wdStrictness: Float = 0.5f, val pixaiStrictness: Float = 0.5f, val maxTags: Int = 30,
    val videoFrames: Int = 6, val batchSize: Int = 8, val vlmParallel: Int = 8, val vramGb: Int = 8, val keepUpdated: Boolean = true,
    val rules: List<RuleDraft> = emptyList(),
) {
    /** The settings as the panel stores them. */
    fun toSettings(): Map<String, JsonElement> = mapOf(
        "instructions" to JsonPrimitive(instructions), "vocabulary" to JsonPrimitive(vocabulary),
        "blocked" to JsonArray(splitTags(blocked).map { JsonPrimitive(it) }), "language" to JsonPrimitive(language.ifBlank { "English" }),
        "describe" to JsonPrimitive(describe), "character_tags" to JsonPrimitive(characterTags), "rating_tag" to JsonPrimitive(ratingTag),
        "write_tags" to JsonPrimitive(writeTags), "use_wd" to JsonPrimitive(useWd), "use_pixai" to JsonPrimitive(usePixai),
        "wd_strictness" to JsonPrimitive(round2(wdStrictness)), "pixai_strictness" to JsonPrimitive(round2(pixaiStrictness)),
        "max_tags" to JsonPrimitive(maxTags), "video_frames" to JsonPrimitive(videoFrames), "batch_size" to JsonPrimitive(batchSize),
        "vlm_parallel" to JsonPrimitive(vlmParallel), "vram_gb" to JsonPrimitive(vramGb), "keep_updated" to JsonPrimitive(keepUpdated),
        "rules" to JsonArray(rules.filter { it.isUsable() }.map { r ->
            buildJsonObject {
                putJsonArray("if_all") { splitTags(r.ifAll).forEach { add(it) } }
                putJsonArray("if_any") { splitTags(r.ifAny).forEach { add(it) } }
                putJsonArray("unless") { splitTags(r.unless).forEach { add(it) } }
                putJsonArray("add") { splitTags(r.add).forEach { add(it) } }
                putJsonArray("remove") { splitTags(r.remove).forEach { add(it) } }
            }
        }),
    )

    companion object {
        fun from(s: JsonObject) = TagDraft(
            instructions = s.str("instructions"), vocabulary = s.str("vocabulary"),
            blocked = s.a("blocked").strings().joinToString(", "), language = s.str("language", "English"),
            describe = s.b("describe") ?: true, characterTags = s.b("character_tags") ?: true, ratingTag = s.b("rating_tag") ?: true,
            writeTags = s.b("write_tags") ?: false, useWd = s.b("use_wd") ?: true, usePixai = s.b("use_pixai") ?: true,
            wdStrictness = (s.d("wd_strictness") ?: 0.5).toFloat(), pixaiStrictness = (s.d("pixai_strictness") ?: 0.5).toFloat(),
            maxTags = s.i("max_tags") ?: 30, videoFrames = s.i("video_frames") ?: 6, batchSize = s.i("batch_size") ?: 8,
            vlmParallel = s.i("vlm_parallel") ?: 8, vramGb = s.i("vram_gb") ?: 8, keepUpdated = s.b("keep_updated") ?: true,
            rules = s.a("rules").objects().map { r ->
                RuleDraft(r.a("if_all").strings().joinToString(", "), r.a("if_any").strings().joinToString(", "),
                    r.a("unless").strings().joinToString(", "), r.a("add").strings().joinToString(", "), r.a("remove").strings().joinToString(", "))
            },
        )

        fun splitTags(text: String): List<String> = text.split(',').map { it.trim() }.filter { it.isNotEmpty() }
        private fun round2(f: Float): Double = Math.round(f * 100) / 100.0
    }
}

private fun RuleDraft.isUsable() =
    (TagDraft.splitTags(ifAll).isNotEmpty() || TagDraft.splitTags(ifAny).isNotEmpty()) &&
        (TagDraft.splitTags(add).isNotEmpty() || TagDraft.splitTags(remove).isNotEmpty())

/** Which re-processing a change calls for (see docs/AI-TAGGER.md, "Reprocess modes"). */
private val FULL_KEYS = setOf("video_frames", "use_wd", "use_pixai")
private val DESCRIBE_KEYS = setOf("instructions", "vocabulary", "language", "describe")
private val RETAG_KEYS = setOf("wd_strictness", "pixai_strictness", "rules", "blocked", "max_tags", "write_tags", "character_tags", "rating_tag")

class TaggerVm : ViewModel() {
    var status by mutableStateOf<JsonObject?>(null)
    var busy by mutableStateOf<String?>(null)
    var tab by mutableIntStateOf(0)
    var draft by mutableStateOf<TagDraft?>(null)
    var testId by mutableStateOf("")
    var preview by mutableStateOf<JsonObject?>(null)
    var items by mutableStateOf<List<Asset>>(emptyList())
    var itemTags by mutableStateOf<Map<String, List<String>>>(emptyMap())
    var total by mutableIntStateOf(0)
    var page by mutableIntStateOf(1)
    var topTags by mutableStateOf<List<Pair<String, Int>>>(emptyList())
    var tagFilter by mutableStateOf("")
    var query by mutableStateOf("")
    var outdatedOnly by mutableStateOf(false)
    val selection = Selection()

    val settings: JsonObject get() = status?.o("settings") ?: JsonObject(emptyMap())

    fun refresh() {
        viewModelScope.launch {
            status = runCatching { Graph.api.get("/api/aitagger").obj() }.getOrElse {
                if (status == null) UiBus.error(it, "ai tagger status")
                status
            }
            if (draft == null) status?.let { draft = TagDraft.from(it.o("settings")) }
        }
    }

    fun action(path: String, body: JsonObject = JsonObject(emptyMap()), message: String? = null, wait: String? = null, onDone: (JsonObject) -> Unit = {}) {
        if (wait != null) busy = wait
        viewModelScope.launch {
            try {
                val data = Graph.api.post("/api/aitagger/$path", body).obj()
                if (data.containsKey("settings")) status = data
                message?.let { UiBus.toast(it) }
                onDone(data)
            } catch (e: Exception) {
                UiBus.error(e, "ai tagger $path")
            } finally {
                if (wait != null) busy = null
            }
        }
    }

    /** Keys whose value in the draft differs from what the panel has. */
    fun changedKeys(): Map<String, JsonElement> {
        val d = draft ?: return emptyMap()
        val now = settings
        return d.toSettings().filter { (k, v) -> now[k]?.toString() != v.toString() && !(now[k] == null && v.toString() == "\"\"") }
    }

    fun suggestedMode(keys: Set<String>): String = when {
        keys.any { it in FULL_KEYS } -> "full"
        keys.any { it in DESCRIBE_KEYS } -> "describe"
        keys.any { it in RETAG_KEYS } -> "retag"
        else -> "none"
    }

    fun save(changes: Map<String, JsonElement>, reprocess: String) {
        action("settings", buildJsonObject {
            put("changes", JsonObject(changes))
            put("reprocess", reprocess)
            put("scope", "all")
        }, if (reprocess == "none") "Saved. New settings apply to newly tagged assets." else "Saved. Already-tagged assets were queued again.") {
            draft = TagDraft.from(it.o("settings"))
        }
    }

    fun sample(type: String) {
        viewModelScope.launch {
            try {
                testId = Graph.api.get("/api/aitagger/sample?type=$type").obj().str("id")
                runPreview()
            } catch (e: Exception) {
                UiBus.error(e, "sample")
            }
        }
    }

    fun runPreview(write: Boolean = false) {
        val id = testId.trim()
        if (!Regex("^[0-9a-fA-F-]{36}$").matches(id)) return UiBus.error("Paste an asset ID, or pick a random photo or video.")
        val loaded = status?.o("service")?.o("vlm")?.s("status") == "ok"
        action(if (write) "apply" else "preview", buildJsonObject { put("id", id) },
            if (write) "Written to the asset's description." else null,
            wait = if (loaded) "Tagging…" else "Loading the models, then tagging… (a few minutes the first time)") {
            preview = it
        }
    }

    fun loadAssets(reset: Boolean = true) {
        if (reset) page = 1
        viewModelScope.launch {
            try {
                val q = buildString {
                    append("/api/aitagger/assets?page=$page&size=60")
                    if (tagFilter.isNotEmpty()) append("&tag=" + URLEncoder.encode(tagFilter, "UTF-8"))
                    if (query.isNotBlank()) append("&q=" + URLEncoder.encode(query.trim(), "UTF-8"))
                    if (outdatedOnly) append("&outdated=1")
                }
                val data = Graph.api.get(q).obj()
                val list = data.a("items").objects()
                val assets = list.mapNotNull { Asset.from(it) }
                items = if (reset) assets else items + assets
                itemTags = (if (reset) emptyMap() else itemTags) + list.associate { it.str("id") to it.a("tags").strings() }
                total = data.i("total") ?: items.size
                topTags = data.a("tags").objects().map { it.str("tag") to (it.i("count") ?: 0) }
            } catch (e: Exception) {
                UiBus.error(e, "tagged assets")
            }
        }
    }

    fun reprocess(mode: String) {
        val ids = selection.ids(items)
        if (ids.isEmpty()) return UiBus.error("Tick some assets first.")
        action("reprocess", buildJsonObject {
            put("scope", "ids"); put("mode", mode)
            putJsonArray("ids") { ids.forEach { add(it) } }
        }, "Queued ${ids.size.plural("asset")} (${mode}).")
    }

    fun remove(exclude: Boolean) {
        val ids = selection.ids(items)
        if (ids.isEmpty()) return UiBus.error("Tick some assets first.")
        action("remove", buildJsonObject {
            put("exclude", exclude)
            putJsonArray("ids") { ids.forEach { add(it) } }
        }, "Removed the AI text from ${ids.size.plural("asset")}.") {
            selection.clear()
            loadAssets()
        }
    }
}

@Composable
fun TaggerScreen(nav: NavController) {
    val vm: TaggerVm = viewModel()
    LaunchedEffect(Unit) {
        while (true) {
            vm.refresh()
            delay(5000)
        }
    }
    LaunchedEffect(vm.tab) { if (vm.tab == 2) vm.loadAssets() }
    val models = vm.status?.o("models")
    Scaffold(topBar = {
        ScreenTop("AI Tagger", nav, subtitle = models?.let { "${it.str("wd")} · ${it.str("pixai")} · ${it.str("vlm")}" } ?: "Tags and describes your library")
    }) { padding ->
        Box(Modifier.fillMaxSize().padding(padding)) {
            LazyVerticalGrid(
                columns = GridCells.Adaptive(Graph.settings.tileDp.dp),
                contentPadding = PaddingValues(start = 10.dp, end = 10.dp, top = 4.dp, bottom = 40.dp),
                horizontalArrangement = Arrangement.spacedBy(4.dp),
                verticalArrangement = Arrangement.spacedBy(8.dp),
                modifier = Modifier.fillMaxSize(),
            ) {
                fullSpan("tabs") {
                    PrimaryTabRow(selectedTabIndex = vm.tab) {
                        listOf("Run", "How to tag", "Tagged").forEachIndexed { i, t ->
                            Tab(selected = vm.tab == i, onClick = { vm.tab = i }, text = { Text(t) })
                        }
                    }
                }
                when (vm.tab) {
                    0 -> {
                        fullSpan("status") { StatusCard(vm) }
                        fullSpan("test") { TestCard(vm) }
                    }
                    1 -> fullSpan("settings") { SettingsCards(vm) }
                    else -> {
                        fullSpan("filters") { TaggedHeader(vm) }
                        assetItems(vm.items, vm.selection, label = { _, a -> vm.itemTags[a.id]?.take(2)?.joinToString(", ") }) { index ->
                            ViewerSession.open(vm.items, index, vm.selection, "AI Tagger")
                            nav.navigate("viewer")
                        }
                        if (vm.items.size < vm.total) {
                            fullSpan("more") {
                                TextButton(onClick = { vm.page++; vm.loadAssets(reset = false) }, modifier = Modifier.fillMaxWidth()) {
                                    Text("Load more (${vm.items.size} of ${vm.total})")
                                }
                            }
                        }
                    }
                }
            }
            BusyLayer(vm.busy)
        }
    }
}

private fun stateText(ix: JsonObject, c: JsonObject): String = when (ix.s("state")) {
    "running" -> ix.d("ratePerMin")?.takeIf { it > 0 }?.let { "tagging %,d/min".format(it.toInt()) } ?: "tagging"
    "starting" -> ix.str("detail", "starting")
    "done" -> "up to date"
    "error" -> "stopped: ${ix.str("detail").ifEmpty { ix.str("error") }}"
    else -> if ((c.i("processed") ?: 0) > 0) "paused" else "not started"
}

private fun serviceText(sv: JsonObject, name: String): String = when {
    sv.s("status") == "ok" -> "$name ready"
    sv.s("status") == "error" -> "$name failed: ${sv.str("error")}"
    sv.s("container") == "running" -> "$name loading…"
    sv.s("container") == "missing" -> "$name not installed"
    else -> "$name off"
}

@Composable
private fun StatusCard(vm: TaggerVm) {
    val st = vm.status
    SectionCard(title = "Progress") {
        if (st == null) {
            Text("Loading…")
            return@SectionCard
        }
        val c = st.o("counts")
        val ix = st.o("indexer")
        val sv = st.o("service")
        val indexing = vm.settings.b("indexing") == true
        Text(stateText(ix, c).replaceFirstChar { it.uppercase() }, style = MaterialTheme.typography.titleMedium)
        val assets = c.i("assets") ?: 0
        val done = c.i("processed") ?: 0
        LinearProgressIndicator(progress = { if (assets > 0) done.toFloat() / assets else 0f }, modifier = Modifier.fillMaxWidth())
        Text("%,d of %,d tagged".format(done, assets) +
            (ix.i("etaMinutes")?.takeIf { (c.i("pending") ?: 0) > 0 }?.let { " · about ${if (it >= 120) "${it / 60} h" else "$it min"} left" } ?: ""))
        Hint("Still to do %,d · queued again %,d · made with older settings %,d · failed %,d · skipped %,d".format(
            c.i("pending") ?: 0, c.i("queued") ?: 0, c.i("outdated") ?: 0, c.i("failed") ?: 0, c.i("excluded") ?: 0))
        val gpu = sv.o("gpu")
        Hint(serviceText(sv.o("tagger"), "Taggers") + " · " + serviceText(sv.o("vlm"), "Describer") +
            (gpu.d("usedGb")?.let { " · GPU %.1f of %.0f GB in use".format(it, gpu.d("totalGb") ?: 24.0) } ?: ""))
        Hint("Search+ is paused while the AI Tagger uses the GPU.")
        FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
            Button(onClick = {
                vm.action("index", buildJsonObject { put("action", if (indexing) "pause" else "start") },
                    if (indexing) "Paused." else "Tagging started. The models load first (a few minutes the first time).")
            }) { Text(if (indexing) "Pause" else if (done > 0) "Resume tagging" else "Start tagging") }
            FilledTonalButton(onClick = { vm.action("load", message = "Loading the models…") }) { Text("Load models") }
            OutlinedButton(onClick = { vm.action("unload", message = "Stopped; the GPU memory is free.") }) { Text("Stop & free GPU") }
        }
        st.a("failures").objects().takeIf { it.isNotEmpty() }?.let { failures ->
            Text("Could not tag ${(c.i("failed") ?: 0).plural("item")}", style = MaterialTheme.typography.titleSmall)
            failures.forEach { f ->
                Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    AsyncImage(model = Graph.api.thumbUrl(f.str("id")), contentDescription = null, modifier = Modifier.size(40.dp))
                    Text(f.str("error"), style = MaterialTheme.typography.bodySmall)
                }
            }
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                FilledTonalButton(onClick = { vm.action("index", buildJsonObject { put("action", "retry") }, "They will be tried again.") }) { Text("Try again") }
                FilledTonalButton(onClick = { vm.action("index", buildJsonObject { put("action", "clear") }, "Cleared.") }) { Text("Clear list") }
            }
        }
    }
}

@Composable
private fun TestCard(vm: TaggerVm) {
    SectionCard(title = "Try it on one asset") {
        Hint("Runs the whole pipeline on one photo or video and shows every step. Nothing is written unless you press “Write this”.")
        Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            if (vm.testId.length == 36) AsyncImage(model = Graph.api.thumbUrl(vm.testId), contentDescription = null, modifier = Modifier.size(56.dp))
            OutlinedTextField(value = vm.testId, onValueChange = { vm.testId = it.trim() }, label = { Text("Asset ID") },
                singleLine = true, modifier = Modifier.weight(1f))
        }
        FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            FilledTonalButton(onClick = { vm.sample("IMAGE") }) { Text("Random photo") }
            FilledTonalButton(onClick = { vm.sample("VIDEO") }) { Text("Random video") }
            Button(onClick = { vm.runPreview() }) { Text("Preview") }
        }
        vm.preview?.let { PreviewResult(vm, it) }
    }
}

@Composable
private fun PreviewResult(vm: TaggerVm, p: JsonObject) {
    val frames = p.a("frames").strings()
    if (frames.isNotEmpty()) {
        Text("Captures (${p.i("captures") ?: frames.size})", style = MaterialTheme.typography.titleSmall)
        Row(Modifier.horizontalScroll(rememberScrollState()), horizontalArrangement = Arrangement.spacedBy(4.dp)) {
            frames.forEach { url ->
                val bitmap = remember(url) {
                    runCatching {
                        val bytes = Base64.decode(url.substringAfter("base64,"), Base64.DEFAULT)
                        BitmapFactory.decodeByteArray(bytes, 0, bytes.size)?.asImageBitmap()
                    }.getOrNull()
                }
                bitmap?.let { Image(it, contentDescription = null, contentScale = ContentScale.Crop, modifier = Modifier.size(84.dp)) }
            }
        }
    }
    val m = p.o("models")
    TagChips("WD tagger", m.a("wd").objects().map { it.str("tag") to it.d("score") })
    TagChips("PixAI", m.a("pixai").objects().map { it.str("tag") to it.d("score") })
    m.o("rating").takeIf { it.isNotEmpty() }?.let { r ->
        Hint("Rating: " + r.entries.sortedByDescending { (it.value as? JsonPrimitive)?.content?.toDoubleOrNull() ?: 0.0 }
            .joinToString(" · ") { "${it.key} %.2f".format((it.value as? JsonPrimitive)?.content?.toDoubleOrNull() ?: 0.0) })
    }
    val v = p.o("vlm")
    if (v.isNotEmpty()) {
        val added = v.a("add_tags").strings()
        val removed = v.a("remove_tags").strings()
        if (added.isNotEmpty() || removed.isNotEmpty()) Hint("Describer added: ${added.joinToString(", ").ifEmpty { "–" }} · removed: ${removed.joinToString(", ").ifEmpty { "–" }}")
        v.s("note")?.takeIf { it.isNotBlank() }?.let { Hint("Describer: $it") }
    }
    p.a("rules").objects().filter { it.a("added").isNotEmpty() || it.a("removed").isNotEmpty() }.forEach { r ->
        Hint("Rule ${(r.i("rule") ?: 0) + 1}: +${r.a("added").strings().joinToString(", ")} −${r.a("removed").strings().joinToString(", ")}")
    }
    TagChips("Final tags", p.a("tags").objects().map { "${it.str("tag")} (${it.str("source")})" to null })
    Text("Before", style = MaterialTheme.typography.titleSmall)
    Text(p.str("currentDescription").ifEmpty { "(empty)" }, fontFamily = FontFamily.Monospace, fontSize = 12.sp)
    Text("After", style = MaterialTheme.typography.titleSmall)
    Text(p.str("newDescription"), fontFamily = FontFamily.Monospace, fontSize = 12.sp)
    if (p.b("written") == true) Hint("Written to Immich.")
    else Button(onClick = { vm.runPreview(write = true) }, modifier = Modifier.fillMaxWidth()) { Text("Write this") }
}

@Composable
private fun TagChips(title: String, tags: List<Pair<String, Double?>>) {
    if (tags.isEmpty()) return
    Text(title, style = MaterialTheme.typography.titleSmall)
    FlowRow(horizontalArrangement = Arrangement.spacedBy(4.dp)) {
        tags.forEach { (t, score) ->
            SuggestionChip(onClick = {}, label = { Text(if (score != null) "$t %.2f".format(score) else t, fontSize = 12.sp) })
        }
    }
}

@Composable
private fun SettingsCards(vm: TaggerVm) {
    val d = vm.draft
    if (d == null) {
        Text("Loading…", Modifier.padding(16.dp))
        return
    }
    var ask by remember { mutableStateOf<Map<String, JsonElement>?>(null) }
    val limits = vm.status?.o("limits") ?: JsonObject(emptyMap())
    fun range(key: String, lo: Int, hi: Int): IntRange {
        val l = limits.a(key)
        return ((l.getOrNull(0) as? JsonPrimitive)?.content?.toDoubleOrNull()?.toInt() ?: lo)..((l.getOrNull(1) as? JsonPrimitive)?.content?.toDoubleOrNull()?.toInt() ?: hi)
    }
    Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
        SectionCard(title = "Instructions") {
            OutlinedTextField(value = d.instructions, onValueChange = { vm.draft = d.copy(instructions = it) },
                label = { Text("How to word the description and pick tags") }, minLines = 4, modifier = Modifier.fillMaxWidth())
            Hint("Plain words for the small text model that writes the description from the tags, e.g. “Mention the setting. Call 1girl a woman.” It does not see the picture, only the tags.")
            OutlinedTextField(value = d.vocabulary, onValueChange = { vm.draft = d.copy(vocabulary = it) },
                label = { Text("Vocabulary") }, minLines = 3, modifier = Modifier.fillMaxWidth())
            Hint("One per line. “old -> new” renames a tag (e.g. “1girl -> woman”); any other line is a term to prefer.")
            OutlinedTextField(value = d.blocked, onValueChange = { vm.draft = d.copy(blocked = it) },
                label = { Text("Never use these tags (comma separated)") }, modifier = Modifier.fillMaxWidth())
            OutlinedTextField(value = d.language, onValueChange = { vm.draft = d.copy(language = it) },
                label = { Text("Language of the description") }, singleLine = true, modifier = Modifier.fillMaxWidth())
            CheckRow("Short description from the tags (small text model)", d.describe) { vm.draft = d.copy(describe = it) }
            CheckRow("Character names (anime/game)", d.characterTags) { vm.draft = d.copy(characterTags = it) }
            CheckRow("Content rating tag", d.ratingTag) { vm.draft = d.copy(ratingTag = it) }
            CheckRow("Also add real Immich tags (AI/…)", d.writeTags) { vm.draft = d.copy(writeTags = it) }
        }
        SectionCard(title = "Taggers") {
            CheckRow("WD tagger (illustration, people, clothing)", d.useWd) { vm.draft = d.copy(useWd = it) }
            Text("WD strictness %.2f".format(d.wdStrictness))
            Slider(value = d.wdStrictness, onValueChange = { vm.draft = d.copy(wdStrictness = it) }, valueRange = 0.05f..0.95f)
            CheckRow("PixAI (anime/illustration, characters, series)", d.usePixai) { vm.draft = d.copy(usePixai = it) }
            Text("PixAI strictness %.2f".format(d.pixaiStrictness))
            Slider(value = d.pixaiStrictness, onValueChange = { vm.draft = d.copy(pixaiStrictness = it) }, valueRange = 0.05f..0.95f)
            Hint("0.50 is each model's own recommended cut-off. Higher keeps fewer, surer tags.")
            NumberField("Most tags per asset", d.maxTags, { vm.draft = d.copy(maxTags = it) }, Modifier.fillMaxWidth(), max = range("max_tags", 5, 100).last)
        }
        SectionCard(title = "Rules") {
            Hint("When an asset has these tags, add or remove others. Tags separated by commas.")
            d.rules.forEachIndexed { i, r ->
                Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Text("Rule ${i + 1}", style = MaterialTheme.typography.titleSmall, modifier = Modifier.weight(1f))
                        IconButton(onClick = { vm.draft = d.copy(rules = d.rules.filterIndexed { j, _ -> j != i }) }) {
                            Icon(Icons.Filled.Delete, contentDescription = "Delete rule")
                        }
                    }
                    fun set(n: RuleDraft) { vm.draft = d.copy(rules = d.rules.mapIndexed { j, x -> if (j == i) n else x }) }
                    RuleField("If it has all of", r.ifAll) { set(r.copy(ifAll = it)) }
                    RuleField("…or any of", r.ifAny) { set(r.copy(ifAny = it)) }
                    RuleField("Unless it has", r.unless) { set(r.copy(unless = it)) }
                    RuleField("Then add", r.add) { set(r.copy(add = it)) }
                    RuleField("And remove", r.remove) { set(r.copy(remove = it)) }
                    if (!r.isUsable()) Hint("Needs at least one “if” and one “add” or “remove”; until then it is not saved.")
                }
            }
            TextButton(onClick = { vm.draft = d.copy(rules = d.rules + RuleDraft()) }) { Text("Add a rule") }
        }
        SectionCard(title = "Speed and memory") {
            Text("Captures per video")
            Segmented(listOf("2" to "2 captures", "6" to "6 captures"), d.videoFrames.toString(), { vm.draft = d.copy(videoFrames = it.toInt()) })
            val batch = range("batch_size", 1, 64)
            Text("Assets per round: ${d.batchSize}")
            Slider(value = d.batchSize.toFloat(), onValueChange = { vm.draft = d.copy(batchSize = it.toInt()) },
                valueRange = batch.first.toFloat()..batch.last.toFloat())
            val par = range("vlm_parallel", 1, 32)
            Text("Descriptions at the same time: ${d.vlmParallel}")
            Slider(value = d.vlmParallel.toFloat(), onValueChange = { vm.draft = d.copy(vlmParallel = it.toInt()) },
                valueRange = par.first.toFloat()..par.last.toFloat())
            val vram = range("vram_gb", 4, 16)
            Text("GPU memory for the two taggers: ${d.vramGb} GB")
            Slider(value = d.vramGb.toFloat(), onValueChange = { vm.draft = d.copy(vramGb = it.toInt()) },
                valueRange = vram.first.toFloat()..vram.last.toFloat())
            Hint("The description model uses about 3 GB on its own. Changes to memory and parallel descriptions restart the models next time they load.")
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text("Keep it up to date (tag new uploads)", modifier = Modifier.weight(1f))
                Switch(checked = d.keepUpdated, onCheckedChange = { vm.draft = d.copy(keepUpdated = it) })
            }
        }
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Button(onClick = {
                val changes = vm.changedKeys()
                when {
                    changes.isEmpty() -> UiBus.toast("Nothing changed.")
                    vm.suggestedMode(changes.keys) == "none" || (vm.status?.o("counts")?.i("processed") ?: 0) == 0 -> vm.save(changes, "none")
                    else -> ask = changes
                }
            }) { Text("Save") }
            TextButton(onClick = { vm.draft = TagDraft.from(vm.settings) }) { Text("Undo changes") }
        }
    }
    ask?.let { changes ->
        ApplyDialog(vm, changes, onClose = { ask = null })
    }
}

@Composable
private fun RuleField(label: String, value: String, onChange: (String) -> Unit) {
    OutlinedTextField(value = value, onValueChange = onChange, label = { Text(label) }, singleLine = true, modifier = Modifier.fillMaxWidth())
}

@Composable
private fun ApplyDialog(vm: TaggerVm, changes: Map<String, JsonElement>, onClose: () -> Unit) {
    val suggested = vm.suggestedMode(changes.keys)
    val processed = vm.status?.o("counts")?.i("processed") ?: 0
    var choice by remember { mutableStateOf("none") }
    val modeText = when (suggested) {
        "retag" -> "re-tag them (fast, no AI needed)"
        "describe" -> "describe them again with the new instructions"
        else -> "process them again from scratch"
    }
    AlertDialog(
        onDismissRequest = onClose,
        title = { Text("Apply the new settings to…") },
        text = {
            Column {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    RadioButton(selected = choice == "none", onClick = { choice = "none" })
                    Text("New assets only")
                }
                Row(verticalAlignment = Alignment.CenterVertically) {
                    RadioButton(selected = choice == suggested, onClick = { choice = suggested })
                    Text("Also the ${processed.plural("tagged asset")}: $modeText")
                }
            }
        },
        confirmButton = { TextButton(onClick = { onClose(); vm.save(changes, choice) }) { Text("Save") } },
        dismissButton = { TextButton(onClick = onClose) { Text("Cancel") } },
    )
}

@Composable
private fun TaggedHeader(vm: TaggerVm) {
    var confirm by remember { mutableStateOf<Confirm?>(null) }
    var exclude by remember { mutableStateOf(false) }
    Column(verticalArrangement = Arrangement.spacedBy(6.dp)) {
        OutlinedTextField(value = vm.query, onValueChange = { vm.query = it }, label = { Text("Search tags and descriptions") },
            singleLine = true, modifier = Modifier.fillMaxWidth())
        Row(verticalAlignment = Alignment.CenterVertically) {
            FilterChip(selected = vm.outdatedOnly, onClick = { vm.outdatedOnly = !vm.outdatedOnly; vm.loadAssets() }, label = { Text("Older settings only") })
            TextButton(onClick = { vm.loadAssets() }) { Text("Search") }
        }
        if (vm.topTags.isNotEmpty()) {
            Row(Modifier.horizontalScroll(rememberScrollState()), horizontalArrangement = Arrangement.spacedBy(4.dp)) {
                vm.topTags.take(40).forEach { (t, n) ->
                    FilterChip(selected = vm.tagFilter == t, onClick = { vm.tagFilter = if (vm.tagFilter == t) "" else t; vm.loadAssets() },
                        label = { Text("$t $n", fontSize = 12.sp) })
                }
            }
        }
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text("${vm.total.plural("asset")} · ${vm.selection.size} ticked", style = MaterialTheme.typography.titleSmall, modifier = Modifier.weight(1f))
            TextButton(onClick = { vm.selection.selectAll(vm.items.map { it.id }) }) { Text("All") }
            TextButton(onClick = { vm.selection.clear() }) { Text("None") }
        }
        if (vm.selection.size > 0) {
            FlowRow(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                FilledTonalButton(onClick = { vm.reprocess("retag") }) { Text("Re-tag") }
                FilledTonalButton(onClick = { vm.reprocess("describe") }) { Text("Re-describe") }
                FilledTonalButton(onClick = { vm.reprocess("full") }) { Text("From scratch") }
                OutlinedButton(onClick = {
                    confirm = Confirm("Remove the AI text?", "Removes the AI Tagger part of the description from ${vm.selection.size.plural("asset")}. Your own text stays.",
                        "Remove", danger = true) { vm.remove(exclude) }
                }) { Text("Remove AI text") }
            }
            CheckRow("…and don't tag them again", exclude) { exclude = it }
        }
    }
    ConfirmDialog(confirm) { confirm = null }
}
