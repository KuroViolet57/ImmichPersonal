package com.kuroviolet.imagepanel.ui.components

import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateListOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import com.kuroviolet.imagepanel.model.Tags
import com.kuroviolet.imagepanel.model.UiBus
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.serialization.json.JsonObjectBuilder
import kotlinx.serialization.json.add
import kotlinx.serialization.json.put
import kotlinx.serialization.json.putJsonArray

/**
 * The AI tag and description filters of a search form: what is typed, the chosen tags, all or any, the description
 * text, and the line the panel answers with ("Filtered to 312 assets with all of: anthro, wolf. …").
 */
class TagFilterState {
    var input by mutableStateOf("")
    val tags = mutableStateListOf<String>()
    var mode by mutableStateOf("all")
    var description by mutableStateOf("")
    var summary by mutableStateOf<String?>(null)

    /** Tags or description text were asked for: enough for a search on their own. */
    val active: Boolean get() = tags.isNotEmpty() || description.isNotBlank()

    fun addTag(tag: String) {
        if (tag !in tags) {
            if (tags.size >= Tags.MAX_FILTER_TAGS) return UiBus.error("At most ${Tags.MAX_FILTER_TAGS} tags at a time.")
            tags += tag
        }
        input = ""
    }

    /** Add what is typed, if the AI Tagger has such a tag (a typo is caught here, not as "0 results"). */
    suspend fun addTyped() {
        val name = Tags.norm(input)
        if (name.isEmpty()) return
        val page = try {
            Tags.find(name, limit = 200)
        } catch (e: Exception) {
            UiBus.error(e, "tags")
            return
        }
        if (page.tags.any { it.tag == name } || page.total > page.tags.size) addTag(name)
        else UiBus.error("The AI Tagger has no tag “$name”. Pick one from the list that opens as you type.")
    }

    /** The request fields (`tags`, `tagMode`, `description`), only when there is something to say. */
    fun putInto(body: JsonObjectBuilder) {
        if (tags.isNotEmpty()) {
            body.putJsonArray("tags") { tags.forEach { add(it) } }
            body.put("tagMode", mode)
        }
        if (description.isNotBlank()) body.put("description", description.trim())
    }
}

/** A tag box whose drop-down is filled from the panel while you type (most used first, with the count). */
@Composable
fun TagSuggestField(
    value: String,
    onValueChange: (String) -> Unit,
    onPick: (String) -> Unit,
    modifier: Modifier = Modifier,
    label: String = "AI tags",
    onDone: (() -> Unit)? = null,
) {
    var options by remember { mutableStateOf<List<Pair<String, String>>>(emptyList()) }
    LaunchedEffect(value) {
        delay(150)                                   // a keystroke later this is cancelled and asked again
        options = runCatching { Tags.find(value) }.getOrNull()?.tags?.map { it.tag to "%,d".format(it.count) } ?: emptyList()
    }
    SuggestField(
        value = value, onValueChange = onValueChange, label = label, options = options, onPick = onPick,
        modifier = modifier, placeholder = "Start typing a tag", onDone = onDone, filter = false,
    )
}

/** Tags (suggested as you type, as chips, all / any) and the description text: the new fields of a search. */
@Composable
fun TagFilterFields(state: TagFilterState, tagsHint: String) {
    val scope = rememberCoroutineScope()
    TagSuggestField(
        value = state.input, onValueChange = { state.input = it }, onPick = { state.addTag(it) },
        modifier = Modifier.fillMaxWidth(), onDone = { scope.launch { state.addTyped() } },
    )
    ChipRow(state.tags, onRemove = { state.tags.remove(it) })
    if (state.tags.size >= 2) {
        Segmented(listOf("all" to "All of them", "any" to "Any of them"), state.mode, { state.mode = it })
    }
    Hint(tagsHint)
    OutlinedTextField(
        value = state.description, onValueChange = { state.description = it.take(200) },
        label = { Text("Description contains") }, placeholder = { Text("beach holiday") },
        singleLine = true, modifier = Modifier.fillMaxWidth(),
    )
    Hint("Text in the photo's Immich description (the AI Tagger's tags are written there too). Capitals and accents don't matter.")
}
