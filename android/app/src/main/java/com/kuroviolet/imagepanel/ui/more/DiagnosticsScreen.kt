package com.kuroviolet.imagepanel.ui.more

import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.rememberScrollState
import androidx.compose.material3.FilledTonalButton
import androidx.compose.material3.FilterChip
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.navigation.NavController
import com.kuroviolet.imagepanel.BuildConfig
import com.kuroviolet.imagepanel.Graph
import com.kuroviolet.imagepanel.diag.Diag
import com.kuroviolet.imagepanel.model.UiBus
import com.kuroviolet.imagepanel.net.b
import com.kuroviolet.imagepanel.net.str
import com.kuroviolet.imagepanel.ui.components.Hint
import com.kuroviolet.imagepanel.ui.components.ScreenTop
import com.kuroviolet.imagepanel.ui.components.SectionCard
import com.kuroviolet.imagepanel.ui.viewer.Transfers
import kotlinx.coroutines.launch
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

@OptIn(ExperimentalLayoutApi::class)
@Composable
fun DiagnosticsScreen(nav: NavController) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    val revision = Diag.revision.intValue
    var filter by remember { mutableStateOf("all") }
    var test by remember { mutableStateOf<String?>(null) }
    val crash = remember(revision) { Diag.lastCrash() }
    val entries = remember(revision, filter) {
        Diag.snapshot().filter {
            when (filter) {
                "problems" -> it.level == 'W' || it.level == 'E'
                "network" -> it.tag == "net"
                else -> true
            }
        }.takeLast(600).reversed()
    }
    val settings = Graph.settings
    fun report() = Diag.report("Server: ${settings.baseUrl} · key ${settings.maskedToken}" + (test?.let { "\nLast connection test: $it" } ?: ""))

    Scaffold(topBar = { ScreenTop("Diagnostics", nav) }) { padding ->
        LazyColumn(
            Modifier.fillMaxSize().padding(padding), contentPadding = PaddingValues(12.dp),
            verticalArrangement = Arrangement.spacedBy(6.dp), state = rememberLazyListState(),
        ) {
            item {
                SectionCard {
                    Text("Image Panel ${BuildConfig.VERSION_NAME} (${BuildConfig.VERSION_CODE})", style = MaterialTheme.typography.titleMedium)
                    Hint("Built ${SimpleDateFormat("yyyy-MM-dd HH:mm", Locale.US).format(Date(BuildConfig.BUILD_TIME))} · ${Diag.deviceLine()}")
                    Hint("Server ${settings.baseUrl} · key ${settings.maskedToken} · requests ${Diag.requests}, failed ${Diag.failures}")
                    test?.let { Text(it, style = MaterialTheme.typography.bodyMedium) }
                    FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        FilledTonalButton(onClick = {
                            test = "Testing…"
                            scope.launch {
                                val started = System.currentTimeMillis()
                                test = try {
                                    val s = Graph.api.probe(settings.baseUrl, settings.token)
                                    val ms = System.currentTimeMillis() - started
                                    if (s.b("connected") == true) "OK in $ms ms — Immich ${s.str("version")} · ${s.str("user")}"
                                    else "Panel OK in $ms ms, but it can't reach Immich: ${s.str("error")}"
                                } catch (e: Exception) {
                                    "Failed after ${System.currentTimeMillis() - started} ms: ${e.message}"
                                }
                                Diag.i("diag", "connection test: $test")
                            }
                        }) { Text("Test connection") }
                        FilledTonalButton(onClick = {
                            val stamp = SimpleDateFormat("yyyyMMdd-HHmm", Locale.US).format(Date())
                            runCatching { Transfers.shareText(context, "imagepanel-log-$stamp.txt", report()) }
                                .onFailure { UiBus.error(it, "share log") }
                        }) { Text("Share log") }
                        OutlinedButton(onClick = {
                            val cm = context.getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
                            cm.setPrimaryClip(ClipData.newPlainText("Image Panel log", report()))
                            UiBus.toast("Log copied.")
                        }) { Text("Copy") }
                        TextButton(onClick = { Diag.clear() }) { Text("Clear") }
                    }
                }
            }
            if (crash != null) {
                item {
                    SectionCard(title = "The app crashed last time") {
                        Text(crash.lines().take(14).joinToString("\n"), fontFamily = FontFamily.Monospace, fontSize = 11.sp)
                        FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                            FilledTonalButton(onClick = { runCatching { Transfers.shareText(context, "imagepanel-crash.txt", crash) } }) { Text("Share crash report") }
                            TextButton(onClick = { Diag.clearCrash() }) { Text("Dismiss") }
                        }
                    }
                }
            }
            item {
                FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    listOf("all" to "Everything", "problems" to "Problems", "network" to "Network").forEach { (k, label) ->
                        FilterChip(selected = filter == k, onClick = { filter = k }, label = { Text(label) })
                    }
                }
                Hint("Newest first. The log never contains your access key.")
            }
            items(entries) { e ->
                Text(
                    e.line(),
                    fontFamily = FontFamily.Monospace, fontSize = 11.sp,
                    color = when (e.level) {
                        'E' -> MaterialTheme.colorScheme.error
                        'W' -> Color(0xFFE0A030)
                        else -> MaterialTheme.colorScheme.onSurface
                    },
                    modifier = Modifier.horizontalScroll(rememberScrollState()),
                )
            }
        }
    }
}
