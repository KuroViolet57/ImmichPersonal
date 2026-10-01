@file:OptIn(ExperimentalMaterial3Api::class)

package com.kuroviolet.imagepanel.ui.viewer

import android.app.Activity
import android.content.Intent
import android.content.pm.ActivityInfo
import android.net.Uri
import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.windowInsetsPadding
import androidx.compose.foundation.layout.safeDrawing
import androidx.compose.foundation.layout.only
import androidx.compose.foundation.layout.WindowInsetsSides
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.pager.HorizontalPager
import androidx.compose.foundation.pager.rememberPagerState
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.AutoAwesome
import androidx.compose.material.icons.filled.CheckCircle
import androidx.compose.material.icons.filled.Info
import androidx.compose.material.icons.filled.MoreVert
import androidx.compose.material.icons.outlined.Circle
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.ModalBottomSheet
import androidx.compose.material3.Text
import androidx.compose.material3.rememberModalBottomSheetState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateMapOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalView
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.core.view.WindowCompat
import androidx.core.view.WindowInsetsCompat
import androidx.core.view.WindowInsetsControllerCompat
import androidx.navigation.NavController
import com.kuroviolet.imagepanel.Graph
import com.kuroviolet.imagepanel.diag.Diag
import com.kuroviolet.imagepanel.model.Asset
import com.kuroviolet.imagepanel.model.Pending
import com.kuroviolet.imagepanel.model.UiBus
import com.kuroviolet.imagepanel.model.ViewerSession
import com.kuroviolet.imagepanel.net.b
import com.kuroviolet.imagepanel.net.d
import com.kuroviolet.imagepanel.net.i
import com.kuroviolet.imagepanel.net.l
import com.kuroviolet.imagepanel.net.obj
import com.kuroviolet.imagepanel.net.s
import com.kuroviolet.imagepanel.net.str
import com.kuroviolet.imagepanel.ui.goTab
import kotlinx.coroutines.launch
import kotlinx.serialization.json.JsonObject
import java.time.OffsetDateTime
import java.time.format.DateTimeFormatter
import java.time.format.FormatStyle

@Composable
fun ViewerScreen(nav: NavController) {
    val items = ViewerSession.items
    if (items.isEmpty()) {
        LaunchedEffect(Unit) { nav.popBackStack() }
        return
    }
    val context = LocalContext.current
    val view = LocalView.current
    val scope = rememberCoroutineScope()
    val pager = rememberPagerState(initialPage = ViewerSession.start) { items.size }
    var chrome by remember { mutableStateOf(true) }
    var zoomed by remember { mutableStateOf(false) }
    var hd by remember { mutableStateOf(false) }
    var showInfo by remember { mutableStateOf(false) }
    var menu by remember { mutableStateOf(false) }
    var landscape by remember { mutableStateOf(false) }
    val hooks = ViewerSession.hooks
    // Items that arrive without a type (smart album previews) are looked up once, so videos still play.
    val resolved = remember { mutableStateMapOf<String, Asset>() }
    fun at(index: Int): Asset = items[index].let { resolved[it.id] ?: it }
    val current = at(pager.currentPage.coerceIn(0, items.size - 1))

    // Full screen while the viewer is open; everything back as it was afterwards.
    DisposableEffect(Unit) {
        val window = (context as Activity).window
        val controller = WindowCompat.getInsetsController(window, view)
        controller.systemBarsBehavior = WindowInsetsControllerCompat.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE
        controller.hide(WindowInsetsCompat.Type.systemBars())
        onDispose {
            controller.show(WindowInsetsCompat.Type.systemBars())
            context.requestedOrientation = ActivityInfo.SCREEN_ORIENTATION_UNSPECIFIED
        }
    }
    LaunchedEffect(pager.currentPage) { hd = false; zoomed = false }

    Box(Modifier.fillMaxSize().background(Color.Black)) {
        HorizontalPager(
            state = pager,
            userScrollEnabled = !zoomed,
            beyondViewportPageCount = 1,
            key = { items[it].id },
            modifier = Modifier.fillMaxSize(),
        ) { page ->
            val asset = at(page)
            if (asset.type.isEmpty()) {
                LaunchedEffect(asset.id) {
                    val info = runCatching { Graph.api.get("/api/asset/${asset.id}").obj() }.getOrNull()
                    resolved[asset.id] = asset.copy(
                        type = info?.s("type")?.ifEmpty { null } ?: "IMAGE",
                        name = info?.s("name") ?: asset.name,
                        date = info?.s("taken")?.take(10) ?: asset.date,
                    )
                }
            }
            if (asset.isVideo) {
                VideoPage(
                    asset = asset,
                    active = pager.settledPage == page,
                    chrome = chrome,
                    setChrome = { chrome = it },
                    onFullscreen = {
                        landscape = !landscape
                        (context as Activity).requestedOrientation =
                            if (landscape) ActivityInfo.SCREEN_ORIENTATION_SENSOR_LANDSCAPE else ActivityInfo.SCREEN_ORIENTATION_UNSPECIFIED
                    },
                )
            } else {
                val url = when {
                    asset.isAnimated -> Graph.api.originalUrl(asset.id)
                    hd && page == pager.currentPage -> Graph.api.thumbUrl(asset.id, "fullsize")
                    else -> Graph.api.thumbUrl(asset.id, "preview")
                }
                ZoomableImage(
                    url = url,
                    contentDescription = asset.name,
                    onTap = { chrome = !chrome },
                    onZoomChanged = { zoomed = it },
                )
            }
        }

        AnimatedVisibility(chrome, enter = fadeIn(), exit = fadeOut(), modifier = Modifier.align(Alignment.TopCenter)) {
            Row(
                Modifier.fillMaxWidth().background(Color.Black.copy(alpha = 0.55f)).windowInsetsPadding(WindowInsets.safeDrawing.only(WindowInsetsSides.Top + WindowInsetsSides.Horizontal)).padding(horizontal = 4.dp, vertical = 2.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                IconButton(onClick = { nav.popBackStack() }) { Icon(Icons.AutoMirrored.Filled.ArrowBack, "Back", tint = Color.White) }
                Column(Modifier.weight(1f)) {
                    Text("${pager.currentPage + 1} / ${items.size}", color = Color.White.copy(alpha = 0.75f), style = MaterialTheme.typography.labelSmall)
                    Text(
                        listOfNotNull(current.name.ifEmpty { null }, current.date.ifEmpty { null },
                            current.score?.let { "score %.3f".format(it) }).joinToString(" · "),
                        color = Color.White, style = MaterialTheme.typography.bodySmall, maxLines = 1, overflow = TextOverflow.Ellipsis,
                    )
                }
                if (hooks != null) {
                    val on = hooks.isSelected(current.id)
                    IconButton(onClick = { hooks.toggle(current.id) }) {
                        Icon(if (on) Icons.Filled.CheckCircle else Icons.Outlined.Circle, if (on) "Selected" else "Select",
                            tint = if (on) MaterialTheme.colorScheme.primary else Color.White)
                    }
                }
                IconButton(onClick = {
                    Pending.searchPlusLike = current.id
                    nav.popBackStack()
                    nav.goTab("splus")
                }) { Icon(Icons.Filled.AutoAwesome, "Similar (Search+)", tint = Color.White) }
                IconButton(onClick = { showInfo = true }) { Icon(Icons.Filled.Info, "Details", tint = Color.White) }
                Box {
                    IconButton(onClick = { menu = true }) { Icon(Icons.Filled.MoreVert, "More", tint = Color.White) }
                    DropdownMenu(expanded = menu, onDismissRequest = { menu = false }) {
                        if (!current.isVideo && !current.isAnimated) {
                            DropdownMenuItem(text = { Text(if (hd) "Normal quality" else "Full quality (HD)") }, onClick = { menu = false; hd = !hd })
                        }
                        DropdownMenuItem(text = { Text("Like this (Search tab)") }, onClick = {
                            menu = false
                            Pending.searchLike = current.id
                            nav.popBackStack()
                            nav.goTab("search")
                        })
                        DropdownMenuItem(text = { Text("Download original") }, onClick = {
                            menu = false
                            UiBus.toast("Downloading ${current.name}…")
                            scope.launch {
                                try { UiBus.toast("Saved to Downloads/Image Panel/${Transfers.download(context, current)}") }
                                catch (e: Exception) { UiBus.error(e, "download") }
                            }
                        })
                        DropdownMenuItem(text = { Text("Share…") }, onClick = {
                            menu = false
                            scope.launch {
                                try { Transfers.share(context, current) } catch (e: Exception) { UiBus.error(e, "share") }
                            }
                        })
                        DropdownMenuItem(text = { Text("Open in Immich (browser)") }, onClick = {
                            menu = false
                            val immich = Graph.api.base.replace(Regex(":\\d+$"), ":2283")
                            runCatching {
                                context.startActivity(Intent(Intent.ACTION_VIEW, Uri.parse("$immich/photos/${current.id}")))
                            }.onFailure { UiBus.error(it, "open") }
                        })
                    }
                }
            }
        }
    }
    if (showInfo) InfoSheet(current) { showInfo = false }
}

@Composable
private fun InfoSheet(asset: Asset, onClose: () -> Unit) {
    val sheet = rememberModalBottomSheetState()
    var data by remember(asset.id) { mutableStateOf<JsonObject?>(null) }
    var error by remember(asset.id) { mutableStateOf<String?>(null) }
    LaunchedEffect(asset.id) {
        try {
            data = Graph.api.get("/api/asset/${asset.id}").obj()
        } catch (e: Exception) {
            Diag.w("viewer", "details for ${asset.id}: ${e.message}")
            error = e.message
        }
    }
    ModalBottomSheet(onDismissRequest = onClose, sheetState = sheet) {
        Column(
            Modifier.fillMaxWidth().verticalScroll(rememberScrollState()).padding(horizontal = 18.dp).padding(bottom = 22.dp).navigationBarsPadding(),
            verticalArrangement = Arrangement.spacedBy(6.dp),
        ) {
            val a = data
            when {
                error != null -> Text(error ?: "")
                a == null -> Text("Loading…")
                else -> {
                    Line("File", a.str("name"))
                    Line("Taken", a.s("taken")?.let { t ->
                        runCatching { OffsetDateTime.parse(t).format(DateTimeFormatter.ofLocalizedDateTime(FormatStyle.MEDIUM)) }.getOrDefault(t)
                    } ?: "")
                    val size = listOfNotNull(
                        if (a.i("width") != null && a.i("height") != null) "${a.i("width")}×${a.i("height")}" else null,
                        a.l("size")?.let { "%.1f MB".format(it / 1048576.0) },
                        a.d("duration")?.takeIf { it > 0 }?.let { fmtTime((it * 1000).toLong()) },
                    ).joinToString(" · ")
                    Line("Size", size)
                    Line("Type", a.str("mime"))
                    if (a.b("favorite") == true) Line("Favorite", "yes")
                    val desc = a.str("description")
                    if (desc.isNotEmpty()) {
                        Text("Description", style = MaterialTheme.typography.labelLarge)
                        Text(desc, style = MaterialTheme.typography.bodyMedium)
                    }
                    Line("ID", asset.id)
                }
            }
        }
    }
}

@Composable
private fun Line(label: String, value: String) {
    if (value.isEmpty()) return
    Row {
        Text("$label: ", style = MaterialTheme.typography.labelLarge, color = MaterialTheme.colorScheme.onSurfaceVariant)
        Text(value, style = MaterialTheme.typography.bodyMedium)
    }
}
