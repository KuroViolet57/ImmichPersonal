@file:OptIn(ExperimentalMaterial3Api::class)

package com.kuroviolet.imagepanel.ui.components

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.imePadding
import androidx.compose.material3.Button
import androidx.compose.material3.Checkbox
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.ModalBottomSheet
import androidx.compose.material3.Text
import androidx.compose.material3.rememberModalBottomSheetState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import com.kuroviolet.imagepanel.model.RefData

/** What to do with the ticked photos (the panel's "Add / Move to album" card). */
data class FileRequest(
    val album: String,
    val move: Boolean,
    val archive: Boolean,
    val favorite: Boolean,
    val skipNext: Boolean,
)

@Composable
fun FileSheet(
    count: Int,
    allowMove: Boolean = true,
    showSkip: Boolean = true,
    onClose: () -> Unit,
    onConfirm: (FileRequest) -> Unit,
) {
    val state = rememberModalBottomSheetState(skipPartiallyExpanded = true)
    var album by remember { mutableStateOf("") }
    var move by remember { mutableStateOf(false) }
    var archive by remember { mutableStateOf(false) }
    var favorite by remember { mutableStateOf(false) }
    var skip by remember { mutableStateOf(true) }
    var askMove by remember { mutableStateOf(false) }
    ModalBottomSheet(onDismissRequest = onClose, sheetState = state) {
        Column(
            Modifier.fillMaxWidth().padding(horizontal = 18.dp).padding(bottom = 18.dp).navigationBarsPadding().imePadding(),
            verticalArrangement = Arrangement.spacedBy(10.dp),
        ) {
            Text("${count.plural("item")} → album", style = MaterialTheme.typography.titleMedium)
            if (allowMove) {
                Segmented(listOf("add" to "Add — keep in other albums", "move" to "Move"), if (move) "move" else "add", { move = it == "move" })
                Hint(
                    if (move) "They are added here and taken out of every other album they are in. Nothing is deleted; undo from History."
                    else "They stay in any albums they are already in, and are also added here.",
                )
            }
            SuggestField(
                value = album, onValueChange = { album = it },
                label = "Album (existing, or a new name)",
                options = albumOptions(RefData.albums), onPick = { album = it },
            )
            CheckRow("Archive them (hide from the main timeline)", archive) { archive = it }
            CheckRow("Mark as favorite", favorite) { favorite = it }
            if (showSkip) CheckRow("Leave them out of my next searches", skip) { skip = it }
            Button(
                enabled = album.isNotBlank() && count > 0,
                onClick = {
                    if (move) askMove = true
                    else { onClose(); onConfirm(FileRequest(album.trim(), false, archive, favorite, skip)) }
                },
                modifier = Modifier.fillMaxWidth(),
            ) { Text("${if (move) "Move" else "Add"} ${count.plural("item")}") }
        }
    }
    if (askMove) {
        ConfirmDialog(
            Confirm(
                title = "Move ${count.plural("item")}?",
                text = "They will be taken out of every other album they are in and put in “${album.trim()}”. " +
                    "Nothing is deleted, and you can undo it from History.",
                yes = "Move",
            ) { onClose(); onConfirm(FileRequest(album.trim(), true, archive, favorite, skip)) },
        ) { askMove = false }
    }
}

@Composable
fun CheckRow(text: String, checked: Boolean, onChange: (Boolean) -> Unit) {
    Row(verticalAlignment = Alignment.CenterVertically) {
        Checkbox(checked = checked, onCheckedChange = onChange)
        Text(text, style = MaterialTheme.typography.bodyMedium)
    }
}
