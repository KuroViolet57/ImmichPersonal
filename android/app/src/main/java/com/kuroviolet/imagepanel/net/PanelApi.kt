package com.kuroviolet.imagepanel.net

import com.kuroviolet.imagepanel.Settings
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.booleanOrNull
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.doubleOrNull
import kotlinx.serialization.json.intOrNull
import kotlinx.serialization.json.longOrNull
import okhttp3.Interceptor
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import java.io.IOException
import java.net.URLEncoder

/** An error the panel answered with ({"error": "…"}), or a network problem in plain words. */
class ApiException(val code: Int, message: String) : Exception(message)

/** Adds the access key to every request that goes to the panel (thumbnails and videos too). */
class AuthInterceptor(private val settings: () -> Settings) : Interceptor {
    override fun intercept(chain: Interceptor.Chain): Response {
        val s = settings()
        val request = chain.request()
        val base = s.baseUrl
        if (base.isNotEmpty() && s.token.isNotEmpty() && request.url.toString().startsWith(base)) {
            return chain.proceed(request.newBuilder().header("X-Organizer-Token", s.token).build())
        }
        return chain.proceed(request)
    }
}

class PanelApi(private val http: OkHttpClient, private val settings: () -> Settings) {
    private val json = Json { ignoreUnknownKeys = true }
    private val jsonType = "application/json".toMediaType()

    val base: String get() = settings().baseUrl

    fun thumbUrl(id: String, size: String = "thumbnail"): String =
        "$base/thumb/$id" + if (size == "thumbnail") "" else "?size=$size"
    fun videoUrl(id: String): String = "$base/media/$id?kind=video"
    fun originalUrl(id: String): String = "$base/media/$id?kind=original"
    fun personThumbUrl(id: String): String = "$base/thumb/person/$id"
    /** The web panel itself, logged in (for the few things the app leaves to it). */
    fun webPanelUrl(): String = "$base/?t=" + URLEncoder.encode(settings().token, "UTF-8")

    suspend fun get(path: String): JsonElement = call(request(path).get().build())

    suspend fun post(path: String, body: JsonObject = JsonObject(emptyMap())): JsonElement =
        call(request(path).post(body.toString().toRequestBody(jsonType)).build())

    private fun request(path: String): Request.Builder {
        val b = base
        if (b.isEmpty()) throw ApiException(0, "No panel address set — open Settings.")
        return try {
            Request.Builder().url(b + path)
        } catch (e: IllegalArgumentException) {
            throw ApiException(0, "The panel address “$b” isn't a valid web address.")
        }
    }

    /** Same, against an address and key that are not saved yet (the setup screen's test). */
    suspend fun probe(baseUrl: String, token: String): JsonObject {
        val request = try {
            Request.Builder().url("${baseUrl.trimEnd('/')}/api/status").header("X-Organizer-Token", token).get().build()
        } catch (e: IllegalArgumentException) {
            throw ApiException(0, "“$baseUrl” isn't a valid web address.")
        }
        return call(request).obj()
    }

    private suspend fun call(request: Request): JsonElement = withContext(Dispatchers.IO) {
        val response = try {
            http.newCall(request).execute()
        } catch (e: IOException) {
            throw ApiException(0, friendly(e, request.url.host))
        }
        response.use { r ->
            val text = r.body?.string() ?: ""
            val parsed = runCatching { if (text.isBlank()) JsonNull else json.parseToJsonElement(text) }.getOrNull()
            if (!r.isSuccessful) {
                val msg = (parsed as? JsonObject)?.s("error")
                    ?: when (r.code) {
                        401 -> "The access key is wrong or missing (Settings → server)."
                        404 -> "The panel doesn't know this (is it up to date?)."
                        else -> "The panel answered ${r.code}."
                    }
                throw ApiException(r.code, msg)
            }
            parsed ?: throw ApiException(r.code, "The panel sent something that isn't JSON.")
        }
    }

    private fun friendly(e: IOException, host: String): String = when (e) {
        is java.net.UnknownHostException -> "Can't find $host. Is Tailscale (or your Wi-Fi) on?"
        is java.net.ConnectException -> "Can't reach the panel at $host. Is the PC on and the panel running?"
        is java.net.SocketTimeoutException -> "The panel took too long to answer."
        is java.net.NoRouteToHostException -> "No route to $host. Check Tailscale / Wi-Fi."
        else -> "Network problem: ${e.message ?: e.javaClass.simpleName}"
    }
}

// ---------------------------------------------------------------- small JSON helpers

fun JsonElement?.obj(): JsonObject = this as? JsonObject ?: JsonObject(emptyMap())
fun JsonElement?.arr(): JsonArray = this as? JsonArray ?: JsonArray(emptyList())
fun JsonObject.o(key: String): JsonObject = this[key].obj()
fun JsonObject.a(key: String): JsonArray = this[key].arr()
fun JsonObject.s(key: String): String? = (this[key] as? JsonPrimitive)?.takeIf { it !is JsonNull }?.contentOrNull
fun JsonObject.str(key: String, default: String = ""): String = s(key) ?: default
fun JsonObject.i(key: String): Int? = (this[key] as? JsonPrimitive)?.let { it.intOrNull ?: it.doubleOrNull?.toInt() }
fun JsonObject.l(key: String): Long? = (this[key] as? JsonPrimitive)?.let { it.longOrNull ?: it.doubleOrNull?.toLong() }
fun JsonObject.d(key: String): Double? = (this[key] as? JsonPrimitive)?.doubleOrNull
fun JsonObject.b(key: String): Boolean? = (this[key] as? JsonPrimitive)?.booleanOrNull
fun JsonArray.objects(): List<JsonObject> = mapNotNull { it as? JsonObject }
fun JsonArray.strings(): List<String> = mapNotNull { (it as? JsonPrimitive)?.contentOrNull }
