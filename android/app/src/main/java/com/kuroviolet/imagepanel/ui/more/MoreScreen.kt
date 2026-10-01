package com.kuroviolet.imagepanel.ui.more

import android.content.Intent
import android.net.Uri
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.BugReport
import androidx.compose.material.icons.filled.Face
import androidx.compose.material.icons.filled.History
import androidx.compose.material.icons.filled.Language
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.ListItem
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.remember
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.platform.LocalContext
import androidx.navigation.NavController
import com.kuroviolet.imagepanel.BuildConfig
import com.kuroviolet.imagepanel.Graph
import com.kuroviolet.imagepanel.diag.Diag
import com.kuroviolet.imagepanel.model.UiBus
import com.kuroviolet.imagepanel.ui.components.ScreenTop

@Composable
fun MoreScreen(nav: NavController) {
    val context = LocalContext.current
    val crashed = remember { Diag.lastCrash() != null }
    Scaffold(topBar = { ScreenTop("More", subtitle = "Image Panel ${BuildConfig.VERSION_NAME} · ${Graph.settings.baseUrl}") }) { padding ->
        Column(Modifier.fillMaxSize().padding(padding).verticalScroll(rememberScrollState())) {
            Entry("Faces", "Clean up unnamed people: hide the blurry ones, sharper covers", Icons.Filled.Face) { nav.navigate("faces") }
            Entry("History", "Recent album changes, with undo", Icons.Filled.History) { nav.navigate("history") }
            Entry(
                "Diagnostics", if (crashed) "The app crashed last time — the report is here" else "Logs, connection test, share a report",
                Icons.Filled.BugReport,
            ) { nav.navigate("diag") }
            Entry("Settings", "Server, access key, thumbnail size", Icons.Filled.Settings) { nav.navigate("settings") }
            HorizontalDivider()
            Entry("Open the web panel", "The same panel in the browser", Icons.Filled.Language) {
                runCatching { context.startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(Graph.api.webPanelUrl()))) }
                    .onFailure { UiBus.error(it, "open panel") }
            }
        }
    }
}

@Composable
private fun Entry(title: String, subtitle: String, icon: ImageVector, onClick: () -> Unit) {
    ListItem(
        headlineContent = { Text(title) },
        supportingContent = { Text(subtitle) },
        leadingContent = { Icon(icon, contentDescription = null) },
        modifier = Modifier.clickable(onClick = onClick),
    )
}
