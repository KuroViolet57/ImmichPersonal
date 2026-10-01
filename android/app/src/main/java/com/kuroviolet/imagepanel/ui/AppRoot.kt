package com.kuroviolet.imagepanel.ui

import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.consumeWindowInsets
import androidx.compose.foundation.layout.padding
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.AutoAwesome
import androidx.compose.material.icons.filled.AutoFixHigh
import androidx.compose.material.icons.filled.MoreHoriz
import androidx.compose.material.icons.filled.PhotoLibrary
import androidx.compose.material.icons.filled.Search
import androidx.compose.material3.Icon
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.Scaffold
import androidx.compose.material3.SnackbarDuration
import androidx.compose.material3.SnackbarHost
import androidx.compose.material3.SnackbarHostState
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.navigation.NavGraph.Companion.findStartDestination
import androidx.navigation.NavController
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.composable
import androidx.navigation.compose.currentBackStackEntryAsState
import androidx.navigation.compose.rememberNavController
import com.kuroviolet.imagepanel.Graph
import com.kuroviolet.imagepanel.model.PanelPrefs
import com.kuroviolet.imagepanel.model.RefData
import com.kuroviolet.imagepanel.model.UiBus
import com.kuroviolet.imagepanel.ui.albums.AlbumScreen
import com.kuroviolet.imagepanel.ui.albums.AlbumsScreen
import com.kuroviolet.imagepanel.ui.more.DiagnosticsScreen
import com.kuroviolet.imagepanel.ui.more.FacesScreen
import com.kuroviolet.imagepanel.ui.more.HistoryScreen
import com.kuroviolet.imagepanel.ui.more.MoreScreen
import com.kuroviolet.imagepanel.ui.more.SettingsScreen
import com.kuroviolet.imagepanel.ui.more.SmartAlbumEditorScreen
import com.kuroviolet.imagepanel.ui.more.SmartAlbumsScreen
import com.kuroviolet.imagepanel.ui.search.SearchScreen
import com.kuroviolet.imagepanel.ui.setup.SetupScreen
import com.kuroviolet.imagepanel.ui.splus.SearchPlusScreen
import com.kuroviolet.imagepanel.ui.viewer.ViewerScreen

private data class TabItem(val route: String, val label: String, val icon: ImageVector)

private val TABS = listOf(
    TabItem("search", "Search", Icons.Filled.Search),
    TabItem("splus", "Search+", Icons.Filled.AutoAwesome),
    TabItem("albums", "Albums", Icons.Filled.PhotoLibrary),
    TabItem("smart", "Smart", Icons.Filled.AutoFixHigh),
    TabItem("more", "More", Icons.Filled.MoreHoriz),
)

@Composable
fun AppRoot() {
    val settings = Graph.settings
    if (!settings.configured) {
        SetupScreen()
        return
    }
    val nav = rememberNavController()
    val snackbar = remember { SnackbarHostState() }
    LaunchedEffect(Unit) {
        UiBus.messages.collect { (message, isError) ->
            snackbar.currentSnackbarData?.dismiss()
            snackbar.showSnackbar(
                message, withDismissAction = true,
                duration = if (isError) SnackbarDuration.Long else SnackbarDuration.Short,
            )
        }
    }
    LaunchedEffect(settings.baseUrl, settings.token) {
        PanelPrefs.load()
        RefData.loadAlbums()
        RefData.loadPeople()
    }
    val entry by nav.currentBackStackEntryAsState()
    val route = entry?.destination?.route ?: "search"
    val showBar = TABS.any { it.route == route }

    Scaffold(
        contentWindowInsets = WindowInsets(0),
        snackbarHost = { SnackbarHost(snackbar) },
        bottomBar = {
            if (showBar) {
                NavigationBar {
                    TABS.forEach { tab ->
                        NavigationBarItem(
                            selected = route == tab.route,
                            onClick = { nav.goTab(tab.route) },
                            icon = { Icon(tab.icon, contentDescription = null) },
                            label = { Text(tab.label) },
                        )
                    }
                }
            }
        },
    ) { padding ->
        NavHost(
            navController = nav,
            startDestination = "search",
            modifier = Modifier.padding(padding).consumeWindowInsets(padding),
        ) {
            composable("search") { SearchScreen(nav) }
            composable("splus") { SearchPlusScreen(nav) }
            composable("albums") { AlbumsScreen(nav) }
            composable("album/{id}") { back -> AlbumScreen(nav, back.arguments?.getString("id") ?: "") }
            composable("smart") { SmartAlbumsScreen(nav) }
            composable("theme/{id}") { back -> SmartAlbumEditorScreen(nav, back.arguments?.getString("id") ?: "new") }
            composable("faces") { FacesScreen(nav) }
            composable("more") { MoreScreen(nav) }
            composable("history") { HistoryScreen(nav) }
            composable("diag") { DiagnosticsScreen(nav) }
            composable("settings") { SettingsScreen(nav) }
            composable("viewer") { ViewerScreen(nav) }
        }
    }
}

fun NavController.goTab(route: String) {
    navigate(route) {
        popUpTo(graph.findStartDestination().id) { saveState = true }
        launchSingleTop = true
        restoreState = true
    }
}
