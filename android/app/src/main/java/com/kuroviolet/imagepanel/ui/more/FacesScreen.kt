package com.kuroviolet.imagepanel.ui.more

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.lazy.grid.GridCells
import androidx.compose.foundation.lazy.grid.LazyVerticalGrid
import androidx.compose.foundation.lazy.grid.items
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.CheckCircle
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material3.Button
import androidx.compose.material3.FilledTonalButton
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateMapOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.alpha
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import androidx.lifecycle.viewmodel.compose.viewModel
import androidx.navigation.NavController
import coil3.compose.AsyncImage
import com.kuroviolet.imagepanel.Graph
import com.kuroviolet.imagepanel.model.UiBus
import com.kuroviolet.imagepanel.net.a
import com.kuroviolet.imagepanel.net.b
import com.kuroviolet.imagepanel.net.d
import com.kuroviolet.imagepanel.net.i
import com.kuroviolet.imagepanel.net.obj
import com.kuroviolet.imagepanel.net.objects
import com.kuroviolet.imagepanel.net.s
import com.kuroviolet.imagepanel.net.str
import com.kuroviolet.imagepanel.ui.components.BusyLayer
import com.kuroviolet.imagepanel.ui.components.Confirm
import com.kuroviolet.imagepanel.ui.components.ConfirmDialog
import com.kuroviolet.imagepanel.ui.components.Hint
import com.kuroviolet.imagepanel.ui.components.NumberField
import com.kuroviolet.imagepanel.ui.components.ScreenTop
import com.kuroviolet.imagepanel.ui.components.SectionCard
import com.kuroviolet.imagepanel.ui.components.Segmented
import com.kuroviolet.imagepanel.ui.components.fullSpan
import kotlinx.coroutines.launch
import kotlinx.serialization.json.add
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import kotlinx.serialization.json.putJsonArray
import java.time.OffsetDateTime
import java.time.format.DateTimeFormatter
import java.time.format.FormatStyle

data class FacePerson(val id: String, val hidden: Boolean, val faces: Int, val best: Double?)

class FacesVm : ViewModel() {
    var people by mutableStateOf<List<FacePerson>>(emptyList())
    var meta by mutableStateOf("")
    var show by mutableStateOf("visible")
    var cut by mutableIntStateOf(8)
    val selected = mutableStateMapOf<String, Boolean>()
    var busy by mutableStateOf<String?>(null)
    private var loaded = false

    val shown: List<FacePerson>
        get() = people.filter { show == "all" || (if (show == "hidden") it.hidden else !it.hidden) }
    val selectedShown: List<String> get() = shown.filter { selected.containsKey(it.id) }.map { it.id }

    fun loadOnce() { if (!loaded) load() }

    fun load() {
        busy = "Loading people…"
        viewModelScope.launch {
            try {
                val data = Graph.api.get("/api/faces").obj()
                loaded = true
                people = data.a("people").objects().map {
                    FacePerson(it.str("id"), it.b("hidden") == true, it.i("faces") ?: 0, it.d("best"))
                }
                selected.clear()
                meta = if (data.b("measured") == true) {
                    val at = data.s("generated")?.let { g ->
                        runCatching { OffsetDateTime.parse(g).format(DateTimeFormatter.ofLocalizedDateTime(FormatStyle.SHORT)) }.getOrDefault(g)
                    } ?: "?"
                    "Measured $at. Press Re-measure after new uploads."
                } else "Not measured yet (${data.i("unnamed") ?: 0} unnamed people). Press Re-measure — it takes about 20 seconds."
            } catch (e: Exception) { UiBus.error(e, "faces") } finally { busy = null }
        }
    }

    fun toggle(id: String) { if (selected.containsKey(id)) selected.remove(id) else selected[id] = true }

    fun selectBelow() {
        shown.forEach { p -> if (p.best != null && p.best < cut) selected[p.id] = true }
    }

    fun setHidden(hidden: Boolean) {
        val ids = selectedShown
        if (ids.isEmpty()) return
        busy = if (hidden) "Hiding…" else "Unhiding…"
        viewModelScope.launch {
            try {
                val data = Graph.api.post("/api/faces/visibility", buildJsonObject {
                    putJsonArray("ids") { ids.forEach { add(it) } }; put("hidden", hidden)
                }).obj()
                val failed = data.i("failed") ?: 0
                UiBus.toast("${if (hidden) "Hid" else "Unhid"} ${data.i("changed") ?: 0}" + (if (failed > 0) ", $failed failed." else "."))
                val changed = ids.toSet()
                people = people.map { if (it.id in changed) it.copy(hidden = hidden) else it }
                selected.clear()
            } catch (e: Exception) { UiBus.error(e, "faces") } finally { busy = null }
        }
    }

    fun measure() {
        busy = "Measuring every face… (about 20 seconds)"
        viewModelScope.launch {
            try {
                val data = Graph.api.post("/api/faces/measure").obj()
                UiBus.toast("Measured %,d faces of %,d people.".format(data.i("faces") ?: 0, data.i("people") ?: 0))
            } catch (e: Exception) { UiBus.error(e, "measure faces") } finally { busy = null }
            load()
        }
    }

    fun covers() {
        busy = "Updating covers…"
        viewModelScope.launch {
            try {
                val data = Graph.api.post("/api/faces/covers").obj()
                val failed = data.i("failed") ?: 0
                UiBus.toast("Updated ${data.i("changed") ?: 0} covers" + (if (failed > 0) ", $failed failed" else "") +
                    ". Immich redraws them in the background.")
            } catch (e: Exception) { UiBus.error(e, "covers") } finally { busy = null }
        }
    }
}

@OptIn(ExperimentalLayoutApi::class)
@Composable
fun FacesScreen(nav: NavController) {
    val vm: FacesVm = viewModel()
    LaunchedEffect(Unit) { vm.loadOnce() }
    var confirm by remember { mutableStateOf<Confirm?>(null) }
    val shown = vm.shown
    val selectedCount = vm.selectedShown.size
    Scaffold(topBar = {
        ScreenTop("Faces", nav, subtitle = "Clean up unnamed people") {
            IconButton(onClick = { vm.load() }) { Icon(Icons.Filled.Refresh, "Reload") }
        }
    }) { padding ->
        Box(Modifier.fillMaxSize().padding(padding)) {
            LazyVerticalGrid(
                columns = GridCells.Adaptive(92.dp),
                contentPadding = PaddingValues(start = 10.dp, end = 10.dp, top = 4.dp, bottom = 40.dp),
                horizontalArrangement = Arrangement.spacedBy(6.dp),
                verticalArrangement = Arrangement.spacedBy(6.dp),
                modifier = Modifier.fillMaxSize(),
            ) {
                fullSpan("head") {
                    SectionCard {
                        Hint("Every unnamed person, blurriest first. The number is how much fine detail their sharpest face has " +
                            "at the size Immich's recogniser looks at: under ~8 is usually an unrecognisable smudge, above ~20 is " +
                            "almost always clear. Heavily pixelated faces can still score high, so have a look before hiding. Hiding " +
                            "only removes someone from the People page; nothing is deleted and you can unhide any time.")
                        if (vm.meta.isNotEmpty()) Hint(vm.meta)
                        Segmented(listOf("visible" to "Visible", "hidden" to "Hidden", "all" to "Everyone"), vm.show, { vm.show = it })
                        Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                            NumberField("Select everyone below", vm.cut, { vm.cut = it }, Modifier.weight(1f), max = 999)
                            FilledTonalButton(onClick = { vm.selectBelow() }) { Text("Select") }
                        }
                        FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                            Button(
                                enabled = selectedCount > 0,
                                onClick = {
                                    confirm = Confirm("Hide $selectedCount people?", "They disappear from the People page. " +
                                        "Nothing is deleted; you can unhide them here.", "Hide", danger = true) { vm.setHidden(true) }
                                },
                            ) { Text("Hide selected") }
                            OutlinedButton(enabled = selectedCount > 0, onClick = { vm.setHidden(false) }) { Text("Unhide selected") }
                        }
                        FlowRow(horizontalArrangement = Arrangement.spacedBy(4.dp)) {
                            TextButton(onClick = { vm.selected.clear() }) { Text("Clear selection") }
                            TextButton(onClick = { vm.measure() }) { Text("Re-measure") }
                            TextButton(onClick = {
                                confirm = Confirm("Use the sharpest faces?", "Set every unnamed person's cover photo to their " +
                                    "sharpest face. Named people are not changed.", "Update covers") { vm.covers() }
                            }) { Text("Sharpest face as cover") }
                        }
                    }
                }
                fullSpan("count") {
                    Text("%,d people · %,d selected".format(shown.size, selectedCount), style = MaterialTheme.typography.titleSmall,
                        modifier = Modifier.padding(vertical = 6.dp))
                }
                items(shown, key = { it.id }) { p ->
                    FaceTile(p, vm.selected.containsKey(p.id)) { vm.toggle(p.id) }
                }
            }
            BusyLayer(vm.busy)
        }
    }
    ConfirmDialog(confirm) { confirm = null }
}

@Composable
private fun FaceTile(p: FacePerson, selected: Boolean, onClick: () -> Unit) {
    Box(
        Modifier.aspectRatio(1f).clip(RoundedCornerShape(10.dp)).background(MaterialTheme.colorScheme.surfaceVariant)
            .then(if (selected) Modifier.border(3.dp, MaterialTheme.colorScheme.primary, RoundedCornerShape(10.dp)) else Modifier)
            .clickable(onClick = onClick),
    ) {
        AsyncImage(
            model = Graph.api.personThumbUrl(p.id), contentDescription = "Unnamed person", contentScale = ContentScale.Crop,
            modifier = Modifier.fillMaxSize().alpha(if (p.hidden) 0.45f else 1f),
        )
        if (selected) {
            Icon(Icons.Filled.CheckCircle, contentDescription = "Selected", tint = MaterialTheme.colorScheme.primary,
                modifier = Modifier.align(Alignment.TopEnd).padding(4.dp).size(22.dp).background(Color.White, CircleShape))
        }
        Text(
            (p.best?.let { "%.0f".format(it) } ?: "?") + if (p.hidden) " · hidden" else "",
            color = Color.White, fontSize = 11.sp,
            modifier = Modifier.align(Alignment.BottomStart).padding(4.dp)
                .background(Color.Black.copy(alpha = 0.6f), RoundedCornerShape(4.dp)).padding(horizontal = 5.dp, vertical = 1.dp),
        )
    }
}
