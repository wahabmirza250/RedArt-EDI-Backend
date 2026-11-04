import logging
import traceback

from django.db import IntegrityError
from django.db.models import Q
from django.http import Http404
from drf_spectacular.utils import (
    OpenApiExample,
    OpenApiParameter,
    extend_schema,
    extend_schema_view,
)
from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.soft_delete import (
    filter_active_for_list,
    client_error_message,
    get_active_object_or_404,
    get_api_object_or_404,
    hard_delete_permission_error,
    parse_hard_flag,
)

from apps.claim.models import SubmissionBatch
from apps.claim.utils.validators import parse_optional_int
from apps.core.pagination import StandardPagination
from apps.core.utils.responses import error_response, success_response
from apps.edi.models import EDIControlNumber, EDIFile, EDIFileTransferLog
from apps.edi.utils.readiness import collect_batch_readiness_errors
from apps.edi.serializers import (
    AllocateControlNumberSerializer,
    CreateEDIFileFromBatchSerializer,
    EDIControlNumberIdSerializer,
    EDIControlNumberListSerializer,
    EDIControlNumberSerializer,
    EDIFileIdSerializer,
    EDIFileListSerializer,
    EDIFileSerializer,
    EDIFileTransferLogListSerializer,
    EDIFileTransferLogSerializer,
    Generate837PSerializer,
    MarkEDIFileUploadedSerializer,
    QueueEDIFileUploadSerializer,
)
from apps.edi.tasks import upload_edi_file
from apps.edi.utils.handler import Generate837PHandler
from apps.edi.utils.service import (
    allocate_control_numbers,
    create_edi_file_for_batch,
    mark_edi_file_uploaded,
)
from apps.edi.utils.upload import queue_edi_file_upload, run_edi_file_upload, read_edi_file_bytes

logger = logging.getLogger(__name__)

CTRL_TAG = "edi_control_number"
FILE_TAG = "edi_file"


@extend_schema_view(
    get=extend_schema(
        tags=[CTRL_TAG],
        parameters=[
            OpenApiParameter("page", int, required=False),
            OpenApiParameter("page_size", int, required=False),
            OpenApiParameter("include_inactive", str, required=False),
            OpenApiParameter("batch_id", int, required=False),
            OpenApiParameter("environment", str, required=False),
            OpenApiParameter("search", str, required=False),
        ],
        responses={200: EDIControlNumberListSerializer(many=True)},
    ),
    post=extend_schema(
        tags=[CTRL_TAG],
        request=EDIControlNumberSerializer,
        examples=[
            OpenApiExample(
                "Sample control numbers",
                value={
                    "batch": 1,
                    "environment": "TEST",
                    "isa13": "000001234",
                    "gs06": "1001",
                    "is_active": True,
                },
                request_only=True,
            )
        ],
        responses={201: EDIControlNumberIdSerializer},
    ),
)
class EDIControlNumberListCreateAPIView(APIView):
    def get(self, request):
        try:
            rows = EDIControlNumber.objects.with_relations().order_by("-id")
            rows = filter_active_for_list(request, rows)

            batch_id = parse_optional_int(
                request.query_params.get("batch_id"), "batch_id"
            )
            if batch_id:
                rows = rows.filter(batch_id=batch_id)

            environment = request.query_params.get("environment", "").strip()
            if environment:
                rows = rows.filter(environment=environment.upper())

            search = request.query_params.get("search", "").strip()
            if search:
                rows = rows.filter(
                    Q(isa13__icontains=search)
                    | Q(gs06__icontains=search)
                    | Q(batch__batch_number__icontains=search)
                )

            paginator = StandardPagination()
            page = paginator.paginate_queryset(rows, request, view=self)
            data = EDIControlNumberListSerializer(page, many=True).data
            return Response(
                {
                    "success": True,
                    "message": "EDI control numbers retrieved successfully.",
                    "count": paginator.page.paginator.count,
                    "next": paginator.get_next_link(),
                    "previous": paginator.get_previous_link(),
                    "data": data,
                }
            )
        except ValidationError as exc:
            return error_response("Validation failed.", errors=exc.detail)
        except Exception:
            logger.error("List EDI control numbers failed:\n%s", traceback.format_exc())
            return error_response(
                "Unable to list EDI control numbers.",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    def post(self, request):
        try:
            serializer = EDIControlNumberSerializer(data=request.data)
            if not serializer.is_valid():
                return error_response("Validation failed.", errors=serializer.errors)
            row = serializer.save()
            logger.info("Created EDI control number id=%s", row.id)
            return success_response(
                "EDI control number created successfully.",
                data={"id": row.id},
                status_code=status.HTTP_201_CREATED,
            )
        except IntegrityError:
            return error_response(
                "Unable to create EDI control number due to a conflict.",
                status_code=status.HTTP_409_CONFLICT,
            )
        except Exception:
            logger.error(
                "Create EDI control number failed:\n%s", traceback.format_exc()
            )
            return error_response(
                "Unable to create EDI control number.",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


class EDIControlNumberAllocateAPIView(APIView):
    @extend_schema(
        tags=[CTRL_TAG],
        request=AllocateControlNumberSerializer,
        examples=[
            OpenApiExample(
                "Allocate for batch",
                value={"batch_id": 1, "environment": "TEST"},
                request_only=True,
            )
        ],
        responses={201: EDIControlNumberIdSerializer},
    )
    def post(self, request):
        try:
            serializer = AllocateControlNumberSerializer(data=request.data)
            if not serializer.is_valid():
                return error_response("Validation failed.", errors=serializer.errors)
            data = serializer.validated_data
            row, created = allocate_control_numbers(
                batch_id=data["batch_id"],
                environment=data.get("environment"),
                isa13=data.get("isa13"),
                gs06=data.get("gs06"),
            )
            return success_response(
                "EDI control numbers allocated."
                if created
                else "Existing EDI control numbers returned.",
                data={
                    "id": row.id,
                    "isa13": row.isa13,
                    "gs06": row.gs06,
                    "created": created,
                },
                status_code=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
            )
        except ValueError as exc:
            return error_response(client_error_message(exc), status_code=status.HTTP_400_BAD_REQUEST)
        except IntegrityError:
            return error_response(
                "Unable to allocate control numbers due to a conflict.",
                status_code=status.HTTP_409_CONFLICT,
            )
        except Exception:
            logger.error("Allocate control numbers failed:\n%s", traceback.format_exc())
            return error_response(
                "Unable to allocate EDI control numbers.",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


@extend_schema_view(
    get=extend_schema(tags=[CTRL_TAG], responses={200: EDIControlNumberSerializer}),
    put=extend_schema(
        tags=[CTRL_TAG],
        request=EDIControlNumberSerializer,
        responses={200: EDIControlNumberIdSerializer},
    ),
    patch=extend_schema(
        tags=[CTRL_TAG],
        request=EDIControlNumberSerializer,
        responses={200: EDIControlNumberIdSerializer},
    ),
    delete=extend_schema(
        tags=[CTRL_TAG],
        parameters=[
            OpenApiParameter(
                "hard",
                str,
                required=False,
                description="Pass true to permanently delete instead of soft-deactivate.",
            ),
        ],
        responses={200: EDIControlNumberIdSerializer},
    ),
)
class EDIControlNumberDetailAPIView(APIView):
    def get(self, request, pk):
        try:
            row = get_active_object_or_404(
                EDIControlNumber.objects.with_relations(), pk=pk
            )
            return success_response(
                "EDI control number retrieved successfully.",
                data=EDIControlNumberSerializer(row).data,
            )
        except Http404:
            return error_response(
                "EDI control number not found.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        except Exception:
            logger.error(
                "Get EDI control number id=%s failed:\n%s",
                pk,
                traceback.format_exc(),
            )
            return error_response(
                "Unable to retrieve EDI control number.",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    def put(self, request, pk):
        return self._update(request, pk, partial=False)

    def patch(self, request, pk):
        return self._update(request, pk, partial=True)

    def _update(self, request, pk, partial):
        try:
            row = get_active_object_or_404(
                EDIControlNumber.objects.with_relations(), pk=pk
            )
            serializer = EDIControlNumberSerializer(
                row, data=request.data, partial=partial
            )
            if not serializer.is_valid():
                return error_response("Validation failed.", errors=serializer.errors)
            row = serializer.save()
            return success_response(
                "EDI control number updated successfully.",
                data={"id": row.id},
            )
        except Http404:
            return error_response(
                "EDI control number not found.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        except IntegrityError:
            return error_response(
                "Unable to update EDI control number due to a conflict.",
                status_code=status.HTTP_409_CONFLICT,
            )
        except Exception:
            logger.error(
                "Update EDI control number id=%s failed:\n%s",
                pk,
                traceback.format_exc(),
            )
            return error_response(
                "Unable to update EDI control number.",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    def delete(self, request, pk):
        try:
            hard_delete = parse_hard_flag(request)
            denied = hard_delete_permission_error(request, hard_delete)
            if denied is not None:
                return denied
            row = get_api_object_or_404(
                EDIControlNumber.objects.with_relations(), pk=pk,
                hard=hard_delete,
            )
            if hard_delete:
                row_id = row.id
                row.delete()
                return success_response(
                    "EDI control number permanently deleted.",
                    data={"id": row_id},
                )
            if not row.is_active:
                return success_response(
                    "EDI control number is already inactive.",
                    data={"id": row.id},
                )
            row.is_active = False
            row.save(update_fields=["is_active", "updated_at"])
            return success_response(
                "EDI control number deactivated successfully.",
                data={"id": row.id},
            )
        except Http404:
            return error_response(
                "EDI control number not found.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        except Exception:
            logger.error(
                "Delete EDI control number id=%s failed:\n%s",
                pk,
                traceback.format_exc(),
            )
            return error_response(
                "Unable to delete EDI control number.",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


@extend_schema_view(
    get=extend_schema(
        tags=[FILE_TAG],
        parameters=[
            OpenApiParameter("page", int, required=False),
            OpenApiParameter("page_size", int, required=False),
            OpenApiParameter("include_inactive", str, required=False),
            OpenApiParameter("batch_id", int, required=False),
            OpenApiParameter("status", str, required=False),
            OpenApiParameter("transaction_type", str, required=False),
            OpenApiParameter("search", str, required=False),
        ],
        responses={200: EDIFileListSerializer(many=True)},
    ),
    post=extend_schema(
        tags=[FILE_TAG],
        request=EDIFileSerializer,
        examples=[
            OpenApiExample(
                "Sample EDI file",
                value={
                    "batch": 1,
                    "transaction_type": "837P",
                    "filename": "TP123456-837P-20260830145130985-1of1.txt",
                    "file_hash": "FILEHASH123",
                    "path_or_blob_ref": "s3://edi/001.txt",
                    "status": "UPLOADED",
                    "is_active": True,
                },
                request_only=True,
            )
        ],
        responses={201: EDIFileIdSerializer},
    ),
)
class EDIFileListCreateAPIView(APIView):
    def get(self, request):
        try:
            rows = EDIFile.objects.with_relations().order_by("-id")
            rows = filter_active_for_list(request, rows)

            batch_id = parse_optional_int(
                request.query_params.get("batch_id"), "batch_id"
            )
            if batch_id:
                rows = rows.filter(batch_id=batch_id)

            status_filter = request.query_params.get("status", "").strip()
            if status_filter:
                rows = rows.filter(status=status_filter.upper())

            txn = request.query_params.get("transaction_type", "").strip()
            if txn:
                rows = rows.filter(transaction_type=txn.upper())

            search = request.query_params.get("search", "").strip()
            if search:
                rows = rows.filter(
                    Q(filename__icontains=search)
                    | Q(file_hash__icontains=search)
                    | Q(path_or_blob_ref__icontains=search)
                    | Q(batch__batch_number__icontains=search)
                )

            paginator = StandardPagination()
            page = paginator.paginate_queryset(rows, request, view=self)
            data = EDIFileListSerializer(page, many=True).data
            return Response(
                {
                    "success": True,
                    "message": "EDI files retrieved successfully.",
                    "count": paginator.page.paginator.count,
                    "next": paginator.get_next_link(),
                    "previous": paginator.get_previous_link(),
                    "data": data,
                }
            )
        except ValidationError as exc:
            return error_response("Validation failed.", errors=exc.detail)
        except Exception:
            logger.error("List EDI files failed:\n%s", traceback.format_exc())
            return error_response(
                "Unable to list EDI files.",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    def post(self, request):
        try:
            serializer = EDIFileSerializer(data=request.data)
            if not serializer.is_valid():
                return error_response("Validation failed.", errors=serializer.errors)
            row = serializer.save()
            logger.info("Created EDI file id=%s", row.id)
            return success_response(
                "EDI file created successfully.",
                data={"id": row.id},
                status_code=status.HTTP_201_CREATED,
            )
        except IntegrityError:
            return error_response(
                "Unable to create EDI file due to a conflict.",
                status_code=status.HTTP_409_CONFLICT,
            )
        except Exception:
            logger.error("Create EDI file failed:\n%s", traceback.format_exc())
            return error_response(
                "Unable to create EDI file.",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


class EDIFileFromBatchAPIView(APIView):
    @extend_schema(
        tags=[FILE_TAG],
        request=CreateEDIFileFromBatchSerializer,
        examples=[
            OpenApiExample(
                "Create from batch",
                value={
                    "batch_id": 1,
                    "transaction_type": "837P",
                    "file_hash": "FILEHASH123",
                    "path_or_blob_ref": "s3://edi/001.txt",
                    "status": "GENERATED",
                    "allocate_controls": True,
                },
                request_only=True,
            )
        ],
        responses={201: EDIFileIdSerializer},
    )
    def post(self, request):
        try:
            serializer = CreateEDIFileFromBatchSerializer(data=request.data)
            if not serializer.is_valid():
                return error_response("Validation failed.", errors=serializer.errors)
            data = serializer.validated_data
            row = create_edi_file_for_batch(
                batch_id=data["batch_id"],
                transaction_type=data.get("transaction_type"),
                filename=data.get("filename"),
                file_hash=data.get("file_hash"),
                path_or_blob_ref=data.get("path_or_blob_ref"),
                status=data.get("status"),
                allocate_controls=data.get("allocate_controls", True),
            )
            logger.info(
                "Created EDI file id=%s from batch id=%s",
                row.id,
                data["batch_id"],
            )
            return success_response(
                "EDI file created from batch successfully.",
                data={
                    "id": row.id,
                    "filename": row.filename,
                    "control_number_id": row.control_number_id,
                    "status": row.status,
                },
                status_code=status.HTTP_201_CREATED,
            )
        except ValueError as exc:
            return error_response(client_error_message(exc), status_code=status.HTTP_400_BAD_REQUEST)
        except IntegrityError:
            return error_response(
                "Unable to create EDI file due to a conflict.",
                status_code=status.HTTP_409_CONFLICT,
            )
        except Exception:
            logger.error("Create EDI file from batch failed:\n%s", traceback.format_exc())
            return error_response(
                "Unable to create EDI file from batch.",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


@extend_schema_view(
    get=extend_schema(tags=[FILE_TAG], responses={200: EDIFileSerializer}),
    put=extend_schema(
        tags=[FILE_TAG],
        request=EDIFileSerializer,
        responses={200: EDIFileIdSerializer},
    ),
    patch=extend_schema(
        tags=[FILE_TAG],
        request=EDIFileSerializer,
        responses={200: EDIFileIdSerializer},
    ),
    delete=extend_schema(
        tags=[FILE_TAG],
        parameters=[
            OpenApiParameter(
                "hard",
                str,
                required=False,
                description="Pass true to permanently delete instead of soft-deactivate.",
            ),
        ],
        responses={200: EDIFileIdSerializer},
    ),
)
class EDIFileDetailAPIView(APIView):
    def get(self, request, pk):
        try:
            row = get_active_object_or_404(EDIFile.objects.with_relations(), pk=pk)
            return success_response(
                "EDI file retrieved successfully.",
                data=EDIFileSerializer(row).data,
            )
        except Http404:
            return error_response(
                "EDI file not found.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        except Exception:
            logger.error(
                "Get EDI file id=%s failed:\n%s", pk, traceback.format_exc()
            )
            return error_response(
                "Unable to retrieve EDI file.",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    def put(self, request, pk):
        return self._update(request, pk, partial=False)

    def patch(self, request, pk):
        return self._update(request, pk, partial=True)

    def _update(self, request, pk, partial):
        try:
            row = get_active_object_or_404(EDIFile.objects.with_relations(), pk=pk)
            serializer = EDIFileSerializer(
                row, data=request.data, partial=partial
            )
            if not serializer.is_valid():
                return error_response("Validation failed.", errors=serializer.errors)
            row = serializer.save()
            return success_response(
                "EDI file updated successfully.",
                data={"id": row.id},
            )
        except Http404:
            return error_response(
                "EDI file not found.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        except IntegrityError:
            return error_response(
                "Unable to update EDI file due to a conflict.",
                status_code=status.HTTP_409_CONFLICT,
            )
        except Exception:
            logger.error(
                "Update EDI file id=%s failed:\n%s", pk, traceback.format_exc()
            )
            return error_response(
                "Unable to update EDI file.",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    def delete(self, request, pk):
        try:
            hard_delete = parse_hard_flag(request)
            denied = hard_delete_permission_error(request, hard_delete)
            if denied is not None:
                return denied
            row = get_api_object_or_404(EDIFile.objects.with_relations(), pk=pk, hard=hard_delete)
            if hard_delete:
                row_id = row.id
                row.delete()
                return success_response(
                    "EDI file permanently deleted.",
                    data={"id": row_id},
                )
            if not row.is_active:
                return success_response(
                    "EDI file is already inactive.",
                    data={"id": row.id},
                )
            row.is_active = False
            row.save(update_fields=["is_active", "updated_at"])
            return success_response(
                "EDI file deactivated successfully.",
                data={"id": row.id},
            )
        except Http404:
            return error_response(
                "EDI file not found.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        except Exception:
            logger.error(
                "Delete EDI file id=%s failed:\n%s", pk, traceback.format_exc()
            )
            return error_response(
                "Unable to delete EDI file.",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


class EDIFileMarkUploadedAPIView(APIView):
    @extend_schema(
        tags=[FILE_TAG],
        request=MarkEDIFileUploadedSerializer,
        responses={200: EDIFileIdSerializer},
    )
    def post(self, request, pk):
        try:
            serializer = MarkEDIFileUploadedSerializer(data=request.data)
            if not serializer.is_valid():
                return error_response("Validation failed.", errors=serializer.errors)
            data = serializer.validated_data
            row = mark_edi_file_uploaded(
                pk,
                path_or_blob_ref=data.get("path_or_blob_ref"),
                file_hash=data.get("file_hash"),
            )
            return success_response(
                "EDI file marked as uploaded.",
                data={
                    "id": row.id,
                    "status": row.status,
                    "uploaded_at": row.uploaded_at,
                },
            )
        except ValueError as exc:
            return error_response(client_error_message(exc), status_code=status.HTTP_400_BAD_REQUEST)
        except Exception:
            logger.error(
                "Mark EDI file uploaded id=%s failed:\n%s",
                pk,
                traceback.format_exc(),
            )
            return error_response(
                "Unable to mark EDI file as uploaded.",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


class EDIFileGenerate837PAPIView(APIView):
    @extend_schema(
        tags=[FILE_TAG],
        request=Generate837PSerializer,
        examples=[
            OpenApiExample(
                "Generate 837P",
                value={"batch_id": 1, "allocate_controls": True},
                request_only=True,
            )
        ],
        responses={201: EDIFileIdSerializer},
    )
    def post(self, request):
        try:
            serializer = Generate837PSerializer(data=request.data)
            if not serializer.is_valid():
                return error_response("Validation failed.", errors=serializer.errors)
            data = serializer.validated_data
            handler = Generate837PHandler(
                data["batch_id"],
                allocate_controls=data.get("allocate_controls", True),
            )
            edi_file, _payload, _body = handler.generate()
            return success_response(
                "837P generated successfully.",
                data={
                    "id": edi_file.id,
                    "filename": edi_file.filename,
                    "file_hash": edi_file.file_hash,
                    "path_or_blob_ref": edi_file.path_or_blob_ref,
                    "status": edi_file.status,
                    "control_number_id": edi_file.control_number_id,
                },
                status_code=status.HTTP_201_CREATED,
            )
        except ValueError as exc:
            # Readiness raises a multi-line ValueError; also return a list so
            # clients (Swagger/curl) see every fix item, not only the summary line.
            batch_id = (getattr(request, "data", None) or {}).get("batch_id")
            detail_errors = None
            if batch_id is not None:
                batch = SubmissionBatch.objects.filter(pk=batch_id, is_active=True).first()
                if batch is not None:
                    detail_errors = collect_batch_readiness_errors(batch) or None
            return error_response(
                client_error_message(exc),
                errors=detail_errors,
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        except IntegrityError:
            return error_response(
                "Unable to generate 837P due to a conflict.",
                status_code=status.HTTP_409_CONFLICT,
            )
        except Exception:
            logger.error("Generate 837P failed:\n%s", traceback.format_exc())
            return error_response(
                "Unable to generate 837P.",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


class EDIFileQueueUploadAPIView(APIView):
    @extend_schema(
        tags=[FILE_TAG],
        request=QueueEDIFileUploadSerializer,
        examples=[
            OpenApiExample(
                "Queue upload",
                value={"credentials_id": None},
                request_only=True,
            )
        ],
        responses={200: EDIFileIdSerializer, 502: dict},
    )
    def post(self, request, pk):
        try:
            serializer = QueueEDIFileUploadSerializer(data=request.data or {})
            if not serializer.is_valid():
                return error_response("Validation failed.", errors=serializer.errors)
            credentials_id = serializer.validated_data.get("credentials_id")
            # Recover legacy file records created before generated content
            # was persisted for workers on separate Railway filesystems.
            source_file = EDIFile.objects.get(pk=pk, is_active=True)
            try:
                read_edi_file_bytes(source_file)
            except ValueError as exc:
                if "missing on disk" not in str(exc):
                    raise
                source_file, _payload, _body = Generate837PHandler(
                    source_file.batch_id
                ).generate()
            edi_file, attempt, sftp_log, s3_log = queue_edi_file_upload(
                edi_file_id=source_file.pk,
                credentials_id=credentials_id,
            )
            # Render's free web service has no background worker. Run the
            # already-created transport attempt here so a successful API response
            # means the file has actually reached the trading partner.
            result = run_edi_file_upload(
                edi_file_id=edi_file.id,
                attempt=attempt,
                credentials_id=credentials_id,
            )

            if result.get('status') != 'UPLOADED':
                return error_response(
                    'EDI file was not delivered to the trading partner. Review the transfer logs before retrying.',
                    errors={'edi_file_id': edi_file.id, 'attempt': attempt,
                            'transfer_log_ids': [sftp_log.id, s3_log.id]},
                    status_code=status.HTTP_502_BAD_GATEWAY,
                )

            return success_response(
                "EDI file upload completed.",
                data={
                    "id": edi_file.id,
                    "status": result["status"],
                    "attempt": attempt,
                    "sftp_path": result.get("sftp_path"),
                    "s3_uri": result.get("s3_uri"),
                    "transfer_log_ids": [sftp_log.id, s3_log.id],
                },
                status_code=status.HTTP_200_OK,
            )
        except ValueError as exc:
            return error_response(client_error_message(exc), status_code=status.HTTP_400_BAD_REQUEST)
        except Exception:
            logger.error(
                "Queue EDI upload id=%s failed:\n%s", pk, traceback.format_exc()
            )
            return error_response(
                "Unable to queue EDI file upload.",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


class EDIFileTransferLogListAPIView(APIView):
    @extend_schema(
        tags=[FILE_TAG],
        parameters=[
            OpenApiParameter("page", int, required=False),
            OpenApiParameter("page_size", int, required=False),
            OpenApiParameter("edi_file_id", int, required=False),
            OpenApiParameter("channel", str, required=False),
            OpenApiParameter("status", str, required=False),
            OpenApiParameter("include_inactive", str, required=False),
        ],
        responses={200: EDIFileTransferLogListSerializer(many=True)},
    )
    def get(self, request):
        try:
            rows = EDIFileTransferLog.objects.with_relations().order_by("-id")
            rows = filter_active_for_list(request, rows)

            edi_file_id = parse_optional_int(
                request.query_params.get("edi_file_id"), "edi_file_id"
            )
            if edi_file_id:
                rows = rows.filter(edi_file_id=edi_file_id)

            channel = request.query_params.get("channel", "").strip().upper()
            if channel:
                rows = rows.filter(channel=channel)

            status_filter = request.query_params.get("status", "").strip().upper()
            if status_filter:
                rows = rows.filter(status=status_filter)

            paginator = StandardPagination()
            page = paginator.paginate_queryset(rows, request, view=self)
            data = EDIFileTransferLogListSerializer(page, many=True).data
            return Response(
                {
                    "success": True,
                    "message": "Transfer logs retrieved successfully.",
                    "count": paginator.page.paginator.count,
                    "next": paginator.get_next_link(),
                    "previous": paginator.get_previous_link(),
                    "data": data,
                }
            )
        except ValidationError as exc:
            return error_response("Validation failed.", errors=exc.detail)
        except Exception:
            logger.error("List transfer logs failed:\n%s", traceback.format_exc())
            return error_response(
                "Unable to list transfer logs.",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


class EDIFileTransferLogDetailAPIView(APIView):
    @extend_schema(
        tags=[FILE_TAG],
        responses={200: EDIFileTransferLogSerializer},
    )
    def get(self, request, pk):
        try:
            row = get_active_object_or_404(
                EDIFileTransferLog.objects.with_relations(),
                pk=pk,
            )
            return success_response(
                "Transfer log retrieved successfully.",
                data=EDIFileTransferLogSerializer(row).data,
            )
        except Http404:
            return error_response(
                "Transfer log not found.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        except Exception:
            logger.error(
                "Get transfer log id=%s failed:\n%s", pk, traceback.format_exc()
            )
            return error_response(
                "Unable to retrieve transfer log.",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
