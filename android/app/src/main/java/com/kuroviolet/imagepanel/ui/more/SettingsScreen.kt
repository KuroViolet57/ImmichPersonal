package com.kuroviolet.imagepanel.ui.more

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Slider
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableFloatStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.navigation.NavController
import coil3.SingletonImageLoader
import androidx.compose.ui.platform.LocalContext
import com.kuroviolet.imagepanel.Graph
import com.kuroviolet.imagepanel.model.UiBus
import com.kuroviolet.imagepanel.ui.components.Hint
import com.kuroviolet.imagepanel.ui.components.ScreenTop
import com.kuroviolet.imagepanel.ui.components.SectionCard
import com.kuroviolet.imagepanel.ui.setup.ServerForm

@Composable
fun SettingsScreen(nav: NavController) {
    val context = LocalContext.current
    var tile by remember { mutableFloatStateOf(Graph.settings.tileDp.toFloat()) }
    Scaffold(topBar = { ScreenTop("Settings", nav) }) { padding ->
        Column(
            Modifier.fillMaxSize().padding(padding).imePadding().verticalScroll(rememberScrollState()).padding(12.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            SectionCard(title = "Server") {
                ServerForm(onSaved = { UiBus.toast("Saved.") })
            }
            SectionCard(title = "Thumbnails") {
                Text("Smallest thumbnail size: ${tile.toInt()} dp")
                Slider(
                    value = tile, onValueChange = { tile = it }, valueRange = 72f..200f, steps = 15,
                    onValueChangeFinished = { Graph.settings.setTile(tile.toInt()) },
                )
                Hint("Smaller = more photos per row.")
                OutlinedButton(onClick = {
                    SingletonImageLoader.get(context).let { loader ->
                        loader.memoryCache?.clear()
                        loader.diskCache?.clear()
                    }
                    UiBus.toast("Picture cache cleared.")
                }) { Text("Clear the picture cache") }
            }
        }
    }
}
