package com.kuroviolet.imagepanel.ui.setup

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawingPadding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import com.kuroviolet.imagepanel.Graph
import com.kuroviolet.imagepanel.Settings
import com.kuroviolet.imagepanel.diag.Diag
import com.kuroviolet.imagepanel.net.ApiException
import com.kuroviolet.imagepanel.net.b
import com.kuroviolet.imagepanel.net.str
import com.kuroviolet.imagepanel.ui.components.Hint
import kotlinx.coroutines.launch

/** First start (and Settings → server): where the panel is and its access key. */
@Composable
fun SetupScreen(onSaved: (() -> Unit)? = null) {
    Surface(Modifier.fillMaxSize()) {
        Column(
            Modifier.fillMaxSize().safeDrawingPadding().imePadding().verticalScroll(rememberScrollState()).padding(20.dp),
            verticalArrangement = Arrangement.spacedBy(14.dp),
        ) {
            Text("Image Panel", style = MaterialTheme.typography.headlineMedium)
            Hint("Connect the app to your Immich Organizer panel. Copy the panel link from the computer " +
                "(it looks like http://…:8777/?t=…) and paste it below — or share it from your browser to this app.")
            ServerForm(onSaved = onSaved)
        }
    }
}

@Composable
fun ServerForm(onSaved: (() -> Unit)?) {
    val settings = Graph.settings
    var link by remember { mutableStateOf("") }
    var address by remember { mutableStateOf(settings.baseUrl) }
    var key by remember { mutableStateOf(settings.token) }
    var result by remember { mutableStateOf<String?>(null) }
    var working by remember { mutableStateOf(false) }
    val scope = rememberCoroutineScope()

    OutlinedTextField(
        value = link,
        onValueChange = { text ->
            link = text
            Settings.parseLink(text)?.let { (base, k) ->
                address = base
                if (k.isNotEmpty()) key = k
            }
        },
        label = { Text("Paste the panel link") },
        placeholder = { Text("http://100.x.y.z:8777/?t=…") },
        singleLine = true, modifier = Modifier.fillMaxWidth(),
        keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Uri),
    )
    Hint("…or fill in the two parts:")
    OutlinedTextField(
        value = address, onValueChange = { address = it },
        label = { Text("Panel address") }, placeholder = { Text("http://desktop:8777") },
        singleLine = true, modifier = Modifier.fillMaxWidth(),
        keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Uri),
    )
    OutlinedTextField(
        value = key, onValueChange = { key = it.trim() },
        label = { Text("Access key (the part after ?t=)") },
        singleLine = true, modifier = Modifier.fillMaxWidth(),
        visualTransformation = PasswordVisualTransformation(),
    )
    Hint("From outside your home Wi-Fi use the PC's Tailscale address (100.…) or name, like you do for Immich.")
    Button(
        enabled = !working && address.isNotBlank() && key.isNotBlank(),
        onClick = {
            val base = Settings.normalizeAddress(address)
            working = true
            result = "Checking…"
            scope.launch {
                result = try {
                    val status = Graph.api.probe(base, key)
                    Diag.i("setup", "connected to $base")
                    settings.save(base, key)
                    onSaved?.invoke()
                    if (status.b("connected") == true) "Connected. Immich ${status.str("version")} · ${status.str("user")}"
                    else "The panel answered, but it can't reach Immich: ${status.str("error")}"
                } catch (e: ApiException) {
                    Diag.w("setup", "connect to $base failed: ${e.message}")
                    e.message
                } finally {
                    working = false
                }
            }
        },
        modifier = Modifier.fillMaxWidth(),
    ) { Text("Connect") }
    result?.let { Text(it, style = MaterialTheme.typography.bodyMedium) }
    if (settings.configured && onSaved != null) {
        TextButton(onClick = { settings.forget() }) { Text("Forget this server") }
    }
}
