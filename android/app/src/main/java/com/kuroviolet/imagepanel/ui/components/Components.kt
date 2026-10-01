@file:OptIn(ExperimentalMaterial3Api::class, ExperimentalFoundationApi::class, ExperimentalLayoutApi::class)

package com.kuroviolet.imagepanel.ui.components

import androidx.compose.foundation.ExperimentalFoundationApi
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.combinedClickable
import androidx.compose.foundation.interaction.MutableInteractionSource
import androidx.compose.foundation.interaction.PressInteraction
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ColumnScope
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.grid.GridItemSpan
import androidx.compose.foundation.lazy.grid.LazyGridItemScope
import androidx.compose.foundation.lazy.grid.LazyGridScope
import androidx.compose.foundation.lazy.grid.itemsIndexed
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.CheckCircle
import androidx.compose.material.icons.filled.Clear
import androidx.compose.material.icons.outlined.Circle
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Card
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.DatePicker
import androidx.compose.material3.DatePickerDialog
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.InputChip
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.SegmentedButton
import androidx.compose.material3.SegmentedButtonDefaults
import androidx.compose.material3.SingleChoiceSegmentedButtonRow
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.rememberDatePickerState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateMapOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.alpha
import androidx.compose.ui.draw.clip
import androidx.compose.ui.focus.onFocusChanged
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.layout.onGloballyPositioned
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.ui.window.PopupProperties
import androidx.navigation.NavController
import coil3.compose.AsyncImage
import com.kuroviolet.imagepanel.Graph
import com.kuroviolet.imagepanel.model.Asset
import com.kuroviolet.imagepanel.model.ViewerHooks
import java.time.Instant
import java.time.LocalDate
import java.time.ZoneOffset

/** Which assets are ticked in a grid. Observable; also lets the viewer tick and untick. */
class Selection : ViewerHooks {
    private val map = mutableStateMapOf<String, Boolean>()
    val size: Int get() = map.size
    fun has(id: String) = map.containsKey(id)
    override fun isSelected(id: String) = has(id)
    override fun toggle(id: String) {
        if (map.containsKey(id)) map.remove(id) else map[id] = true
    }
    fun set(id: String, on: Boolean) { if (on) map[id] = true else map.remove(id) }
    fun selectAll(ids: Collection<String>) { map.clear(); ids.forEach { map[it] = true } }
    fun clear() = map.clear()
    fun ids(order: List<Asset>): List<String> = order.map { it.id }.filter { map.containsKey(it) }
}

/** A top bar with an optional back arrow. */
@Composable
fun ScreenTop(title: String, nav: NavController? = null, subtitle: String? = null, actions: @Composable () -> Unit = {}) {
    TopAppBar(
        title = {
            Column {
                Text(title, maxLines = 1, overflow = TextOverflow.Ellipsis)
                if (!subtitle.isNullOrEmpty()) {
                    Text(subtitle, style = MaterialTheme.typography.bodySmall, maxLines = 1, overflow = TextOverflow.Ellipsis,
                        color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
            }
        },
        navigationIcon = {
            if (nav != null) IconButton(onClick = { nav.popBackStack() }) {
                Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = "Back")
            }
        },
        actions = { actions() },
    )
}

@Composable
fun SectionCard(modifier: Modifier = Modifier, title: String? = null, content: @Composable ColumnScope.() -> Unit) {
    Card(modifier = modifier.fillMaxWidth()) {
        Column(Modifier.padding(14.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
            if (title != null) Text(title, style = MaterialTheme.typography.titleMedium)
            content()
        }
    }
}

@Composable
fun Hint(text: String, modifier: Modifier = Modifier) {
    Text(text, modifier = modifier, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
}

/** One photo/video tile: tap opens it, the circle (or a long press) ticks it. */
@Composable
fun AssetTile(
    asset: Asset,
    selected: Boolean?,
    label: String?,
    onOpen: () -> Unit,
    onToggle: (() -> Unit)?,
    badge: String? = null,
    dim: Boolean = selected == false,
) {
    Box(
        Modifier
            .aspectRatio(1f)
            .clip(RoundedCornerShape(8.dp))
            .background(MaterialTheme.colorScheme.surfaceVariant)
            .combinedClickable(onClick = onOpen, onLongClick = onToggle),
    ) {
        AsyncImage(
            model = Graph.api.thumbUrl(asset.id),
            contentDescription = asset.name,
            contentScale = ContentScale.Crop,
            modifier = Modifier.fillMaxSize().alpha(if (dim) 0.38f else 1f),
        )
        if (selected != null && onToggle != null) {
            Box(
                Modifier.align(Alignment.TopEnd).size(42.dp).clickable(onClick = onToggle),
                contentAlignment = Alignment.Center,
            ) {
                Surface(shape = CircleShape, color = if (selected) MaterialTheme.colorScheme.primary else Color.Black.copy(alpha = 0.35f)) {
                    Icon(
                        if (selected) Icons.Filled.CheckCircle else Icons.Outlined.Circle,
                        contentDescription = if (selected) "Selected" else "Not selected",
                        tint = if (selected) MaterialTheme.colorScheme.onPrimary else Color.White,
                        modifier = Modifier.size(24.dp),
                    )
                }
            }
        }
        if (badge != null) {
            Text(
                badge, color = Color.White, fontSize = 10.sp,
                modifier = Modifier.align(Alignment.TopStart).padding(4.dp)
                    .background(Color.Black.copy(alpha = 0.6f), RoundedCornerShape(50)).padding(horizontal = 6.dp, vertical = 1.dp),
            )
        }
        val text = listOfNotNull(label, if (asset.isVideo) "▶" else null).joinToString(" ")
        if (text.isNotEmpty()) {
            Text(
                text, color = Color.White, fontSize = 10.sp, fontWeight = FontWeight.Medium,
                modifier = Modifier.align(Alignment.BottomStart).padding(4.dp)
                    .background(Color.Black.copy(alpha = 0.6f), RoundedCornerShape(4.dp)).padding(horizontal = 4.dp, vertical = 1.dp),
            )
        }
    }
}

/** Grid rows for a list of assets (use inside a LazyVerticalGrid). */
fun LazyGridScope.assetItems(
    items: List<Asset>,
    selection: Selection?,
    label: (Int, Asset) -> String? = { i, _ -> "#${i + 1}" },
    badge: (Asset) -> String? = { null },
    onOpen: (Int) -> Unit,
) {
    itemsIndexed(items, key = { _, a -> a.id }) { index, asset ->
        AssetTile(
            asset = asset,
            selected = selection?.has(asset.id),
            label = label(index, asset),
            onOpen = { onOpen(index) },
            onToggle = selection?.let { s -> { s.toggle(asset.id) } },
            badge = badge(asset),
            // Unticked tiles fade only once something is ticked, so a fresh album isn't all grey.
            dim = selection != null && selection.size > 0 && !selection.has(asset.id),
        )
    }
}

/** A grid row that spans the full width (headers, forms). */
fun LazyGridScope.fullSpan(key: String? = null, content: @Composable LazyGridItemScope.() -> Unit) {
    item(key = key, span = { GridItemSpan(maxLineSpan) }, content = content)
}

/**
 * A text box with a drop-down of suggestions (albums, people, tags) that works on phones: it opens
 * when the box is focused, filters as you type, and picking an entry calls [onPick].
 */
@Composable
fun SuggestField(
    value: String,
    onValueChange: (String) -> Unit,
    label: String,
    options: List<Pair<String, String>>,
    onPick: (String) -> Unit,
    modifier: Modifier = Modifier,
    placeholder: String = "",
    onDone: (() -> Unit)? = null,
) {
    var focused by remember { mutableStateOf(false) }
    var dismissed by remember { mutableStateOf(false) }
    var widthPx by remember { mutableStateOf(0) }
    val density = LocalDensity.current
    val q = value.trim().lowercase()
    val matches = remember(q, options) {
        val base = if (q.isEmpty()) options else options.filter { it.first.lowercase().contains(q) }
            .sortedByDescending { it.first.lowercase().startsWith(q) }
        base.take(80)
    }
    val exact = matches.size == 1 && matches[0].first.equals(value.trim(), ignoreCase = true)
    Box(modifier) {
        OutlinedTextField(
            value = value,
            onValueChange = { onValueChange(it); dismissed = false },
            label = { Text(label) },
            placeholder = { if (placeholder.isNotEmpty()) Text(placeholder) },
            singleLine = true,
            keyboardOptions = KeyboardOptions(imeAction = ImeAction.Done),
            keyboardActions = KeyboardActions(onDone = { dismissed = true; onDone?.invoke() }),
            modifier = Modifier.fillMaxWidth()
                .onGloballyPositioned { widthPx = it.size.width }
                .onFocusChanged { focused = it.isFocused; if (it.isFocused) dismissed = false },
        )
        DropdownMenu(
            expanded = focused && !dismissed && matches.isNotEmpty() && !exact,
            onDismissRequest = { dismissed = true },
            properties = PopupProperties(focusable = false),
            modifier = Modifier.heightIn(max = 320.dp).width(with(density) { widthPx.toDp() }),
        ) {
            matches.forEach { (option, note) ->
                DropdownMenuItem(
                    text = {
                        Row(verticalAlignment = Alignment.CenterVertically) {
                            Text(option, modifier = Modifier.weight(1f), maxLines = 1, overflow = TextOverflow.Ellipsis)
                            if (note.isNotEmpty()) {
                                Text(note, style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                            }
                        }
                    },
                    onClick = { dismissed = true; onPick(option) },
                )
            }
        }
    }
}

/** Removable chips (album names, tags, people). */
@Composable
fun ChipRow(items: List<String>, onRemove: (String) -> Unit, label: (String) -> String = { it }) {
    if (items.isEmpty()) return
    FlowRow(horizontalArrangement = Arrangement.spacedBy(6.dp), verticalArrangement = Arrangement.spacedBy(2.dp)) {
        items.forEach { item ->
            InputChip(
                selected = false,
                onClick = { onRemove(item) },
                label = { Text(label(item), maxLines = 1, overflow = TextOverflow.Ellipsis) },
                trailingIcon = { Icon(Icons.Filled.Clear, contentDescription = "Remove", modifier = Modifier.size(16.dp)) },
            )
        }
    }
}

/** A few mutually exclusive choices in one row. */
@Composable
fun Segmented(options: List<Pair<String, String>>, selected: String, onSelect: (String) -> Unit, modifier: Modifier = Modifier) {
    SingleChoiceSegmentedButtonRow(modifier.fillMaxWidth()) {
        options.forEachIndexed { index, (value, text) ->
            SegmentedButton(
                selected = value == selected,
                onClick = { onSelect(value) },
                shape = SegmentedButtonDefaults.itemShape(index = index, count = options.size),
                label = { Text(text, maxLines = 1, overflow = TextOverflow.Ellipsis) },
            )
        }
    }
}

val MEDIA_OPTIONS = listOf("" to "All", "IMAGE" to "Photos", "VIDEO" to "Videos")

/** A date box (YYYY-MM-DD) that opens a calendar; the ✕ clears it. */
@Composable
fun DateField(label: String, value: String, onChange: (String) -> Unit, modifier: Modifier = Modifier) {
    var open by remember { mutableStateOf(false) }
    val interaction = remember { MutableInteractionSource() }
    LaunchedEffect(interaction) {
        interaction.interactions.collect { if (it is PressInteraction.Release) open = true }
    }
    OutlinedTextField(
        value = value, onValueChange = {}, readOnly = true, singleLine = true,
        label = { Text(label) },
        interactionSource = interaction,
        trailingIcon = {
            if (value.isNotEmpty()) IconButton(onClick = { onChange("") }) { Icon(Icons.Filled.Clear, contentDescription = "Clear") }
        },
        modifier = modifier,
    )
    if (open) {
        val initial = runCatching { LocalDate.parse(value).atStartOfDay(ZoneOffset.UTC).toInstant().toEpochMilli() }.getOrNull()
        val state = rememberDatePickerState(initialSelectedDateMillis = initial)
        DatePickerDialog(
            onDismissRequest = { open = false },
            confirmButton = {
                TextButton(onClick = {
                    state.selectedDateMillis?.let { onChange(Instant.ofEpochMilli(it).atZone(ZoneOffset.UTC).toLocalDate().toString()) }
                    open = false
                }) { Text("OK") }
            },
            dismissButton = { TextButton(onClick = { open = false }) { Text("Cancel") } },
        ) { DatePicker(state = state) }
    }
}

/** A number box that only accepts digits. */
@Composable
fun NumberField(label: String, value: Int, onChange: (Int) -> Unit, modifier: Modifier = Modifier, max: Int = 10000) {
    var text by remember(value) { mutableStateOf(value.toString()) }
    OutlinedTextField(
        value = text,
        onValueChange = { t ->
            text = t.filter { it.isDigit() }.take(6)
            text.toIntOrNull()?.let { onChange(it.coerceIn(1, max)) }
        },
        label = { Text(label) }, singleLine = true,
        keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number, imeAction = ImeAction.Done),
        modifier = modifier,
    )
}

/** A dimmed layer with a spinner while something long runs. */
@Composable
fun BusyLayer(text: String?) {
    if (text == null) return
    Box(Modifier.fillMaxSize().background(Color.Black.copy(alpha = 0.45f)).clickable(enabled = true, onClick = {}), contentAlignment = Alignment.Center) {
        Card {
            Row(Modifier.padding(20.dp), verticalAlignment = Alignment.CenterVertically) {
                CircularProgressIndicator(Modifier.size(26.dp), strokeWidth = 3.dp)
                Spacer(Modifier.width(16.dp))
                Text(text, style = MaterialTheme.typography.bodyMedium)
            }
        }
    }
}

/** A yes/no question. Show it by setting a [Confirm] value; null hides it. */
data class Confirm(val title: String, val text: String, val yes: String = "OK", val danger: Boolean = false, val onYes: () -> Unit)

@Composable
fun ConfirmDialog(confirm: Confirm?, onClose: () -> Unit) {
    if (confirm == null) return
    AlertDialog(
        onDismissRequest = onClose,
        title = { Text(confirm.title) },
        text = { Text(confirm.text) },
        confirmButton = {
            TextButton(onClick = { onClose(); confirm.onYes() }) {
                Text(confirm.yes, color = if (confirm.danger) MaterialTheme.colorScheme.error else Color.Unspecified)
            }
        },
        dismissButton = { TextButton(onClick = onClose) { Text("Cancel") } },
    )
}

/** Asks for one line of text (album name, …). */
@Composable
fun TextPrompt(title: String, initial: String, label: String, yes: String, options: List<Pair<String, String>> = emptyList(),
               onClose: () -> Unit, onYes: (String) -> Unit) {
    var text by remember { mutableStateOf(initial) }
    AlertDialog(
        onDismissRequest = onClose,
        title = { Text(title) },
        text = {
            if (options.isEmpty()) {
                OutlinedTextField(value = text, onValueChange = { text = it }, label = { Text(label) }, singleLine = true)
            } else {
                SuggestField(value = text, onValueChange = { text = it }, label = label, options = options, onPick = { text = it })
            }
        },
        confirmButton = { TextButton(enabled = text.isNotBlank(), onClick = { onClose(); onYes(text.trim()) }) { Text(yes) } },
        dismissButton = { TextButton(onClick = onClose) { Text("Cancel") } },
    )
}

fun albumOptions(albums: List<com.kuroviolet.imagepanel.model.Album>): List<Pair<String, String>> =
    albums.map { it.name to "${it.count} items" }

fun Int.plural(word: String): String = "%,d %s%s".format(this, word, if (this == 1) "" else "s")
