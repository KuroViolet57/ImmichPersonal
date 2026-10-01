package com.kuroviolet.imagepanel

import android.content.Intent
import android.os.Build
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.dynamicDarkColorScheme
import androidx.compose.material3.dynamicLightColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.platform.LocalContext
import com.kuroviolet.imagepanel.diag.Diag
import com.kuroviolet.imagepanel.model.UiBus
import com.kuroviolet.imagepanel.ui.AppRoot

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        handleIntent(intent)
        setContent {
            ImagePanelTheme { AppRoot() }
        }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        handleIntent(intent)
    }

    /** A panel link shared to the app (or passed for testing) sets the server up. */
    private fun handleIntent(intent: Intent?) {
        val text = intent?.getStringExtra("panel_link") ?: intent?.getStringExtra(Intent.EXTRA_TEXT) ?: return
        val (base, key) = Settings.parseLink(text) ?: return
        if (key.isEmpty()) {
            UiBus.error("That link has no access key (?t=…). Copy the full panel link from the computer.")
            return
        }
        Graph.settings.save(base, key)
        Diag.i("setup", "panel link received: $base")
        UiBus.toast("Panel set to $base")
    }
}

@Composable
fun ImagePanelTheme(content: @Composable () -> Unit) {
    val dark = isSystemInDarkTheme()
    val context = LocalContext.current
    val scheme = when {
        Build.VERSION.SDK_INT >= 31 && dark -> dynamicDarkColorScheme(context)
        Build.VERSION.SDK_INT >= 31 -> dynamicLightColorScheme(context)
        dark -> darkColorScheme()
        else -> lightColorScheme()
    }
    MaterialTheme(colorScheme = scheme, content = content)
}
