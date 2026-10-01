package com.kuroviolet.imagepanel.ui.viewer

import android.content.ContentValues
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Environment
import android.provider.MediaStore
import androidx.core.content.FileProvider
import com.kuroviolet.imagepanel.Graph
import com.kuroviolet.imagepanel.diag.Diag
import com.kuroviolet.imagepanel.model.Asset
import com.kuroviolet.imagepanel.net.ApiException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.Request
import java.io.File

/** Saving originals to the phone and sharing them. */
object Transfers {
    private fun safeName(asset: Asset): String =
        asset.name.ifEmpty { asset.id }.replace(Regex("[\\\\/:*?\"<>|]"), "_")

    /** Downloads the original into Downloads/Image Panel. Returns the file name. */
    suspend fun download(context: Context, asset: Asset): String = withContext(Dispatchers.IO) {
        val request = Request.Builder().url(Graph.api.originalUrl(asset.id)).build()
        Graph.http.newCall(request).execute().use { r ->
            if (!r.isSuccessful) throw ApiException(r.code, "Download failed (the panel answered ${r.code}).")
            val body = r.body ?: throw ApiException(r.code, "Download failed (empty answer).")
            val name = safeName(asset)
            val resolver = context.contentResolver
            val values = ContentValues().apply {
                put(MediaStore.Downloads.DISPLAY_NAME, name)
                put(MediaStore.Downloads.MIME_TYPE, r.header("Content-Type") ?: "application/octet-stream")
                put(MediaStore.Downloads.RELATIVE_PATH, Environment.DIRECTORY_DOWNLOADS + "/Image Panel")
                put(MediaStore.Downloads.IS_PENDING, 1)
            }
            val uri = resolver.insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, values)
                ?: throw ApiException(0, "Android refused to create the download file.")
            try {
                resolver.openOutputStream(uri)!!.use { out -> body.byteStream().copyTo(out) }
                resolver.update(uri, ContentValues().apply { put(MediaStore.Downloads.IS_PENDING, 0) }, null, null)
            } catch (e: Exception) {
                resolver.delete(uri, null, null)
                throw e
            }
            Diag.i("download", "saved ${asset.id} as Downloads/Image Panel/$name")
            name
        }
    }

    /** Fetches the original into the cache and opens Android's share sheet for it. */
    suspend fun share(context: Context, asset: Asset) {
        val (uri, mime) = withContext(Dispatchers.IO) {
            val dir = File(context.cacheDir, "share").apply { mkdirs() }
            dir.listFiles()?.filter { System.currentTimeMillis() - it.lastModified() > 3_600_000 }?.forEach { it.delete() }
            val file = File(dir, safeName(asset))
            val request = Request.Builder().url(Graph.api.originalUrl(asset.id)).build()
            val mime = Graph.http.newCall(request).execute().use { r ->
                if (!r.isSuccessful) throw ApiException(r.code, "Couldn't fetch the file (${r.code}).")
                file.outputStream().use { out -> r.body!!.byteStream().copyTo(out) }
                r.header("Content-Type") ?: "application/octet-stream"
            }
            FileProvider.getUriForFile(context, "${context.packageName}.files", file) to mime
        }
        val send = Intent(Intent.ACTION_SEND).apply {
            type = mime
            putExtra(Intent.EXTRA_STREAM, uri)
            addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
        }
        context.startActivity(Intent.createChooser(send, "Share ${asset.name}").addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
    }

    /** Shares a text file (the diagnostics report). */
    fun shareText(context: Context, fileName: String, text: String) {
        val dir = File(context.cacheDir, "share").apply { mkdirs() }
        val file = File(dir, fileName).apply { writeText(text) }
        val uri: Uri = FileProvider.getUriForFile(context, "${context.packageName}.files", file)
        val send = Intent(Intent.ACTION_SEND).apply {
            type = "text/plain"
            putExtra(Intent.EXTRA_STREAM, uri)
            putExtra(Intent.EXTRA_SUBJECT, fileName)
            addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
        }
        context.startActivity(Intent.createChooser(send, "Share the log").addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
    }
}
