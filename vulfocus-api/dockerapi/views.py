import socket
from django.http import JsonResponse
from rest_framework import viewsets
from rest_framework.decorators import action
from dockerapi.models import ImageInfo
from dockerapi.serializers import ImageInfoSerializer, ContainerVulSerializer, SysLogSerializer
from dockerapi.models import ContainerVul
from vulfocus.settings import VUL_IP
import django.utils
import django.utils.timezone as timezone
from .common import R
from django.db.models import Q
from .models import SysLog
import json
from tasks import tasks
from tasks.models import TaskInfo


def get_request_ip(request):
    """
    获取请求IP
    :param request:
    :return:
    """
    request_ip = ""
    if request.META.get('HTTP_X_FORWARDED_FOR'):
        request_ip = request.META.get("HTTP_X_FORWARDED_FOR")
    else:
        request_ip = request.META.get("REMOTE_ADDR")
    return request_ip


class ImageInfoViewSet(viewsets.ModelViewSet):
    serializer_class = ImageInfoSerializer

    def get_queryset(self):
        query = self.request.GET.get("query", "")
        flag = self.request.GET.get("flag", "")
        user = self.request.user
        if user.is_superuser:
            if query:
                query = query.strip()
                if flag and flag == "flag":
                    image_info_list = ImageInfo.objects.filter(Q(image_name__contains=query) | Q(image_vul_name__contains=query)
                                                       | Q(image_desc__contains=query))
                else:
                    image_info_list = ImageInfo.objects.filter(Q(image_name__contains=query) | Q(image_vul_name__contains=query)
                                                       | Q(image_desc__contains=query),is_ok=True)
            else:
                if flag and flag == "flag":
                    image_info_list = ImageInfo.objects.filter()
                else:
                    image_info_list = ImageInfo.objects.filter(is_ok=True)
        else:
            if query:
                query = query.strip()
                image_info_list = ImageInfo.objects.filter(Q(image_name__contains=query) | Q(image_vul_name__contains=query)
                                                       | Q(image_desc__contains=query),is_ok=True)
            else:
                image_info_list = ImageInfo.objects.filter(is_ok=True)
        return image_info_list

    def destroy(self, request, *args, **kwargs):
        return JsonResponse(R.ok())

    def create(self, request, *args, **kwargs):
        """
        创建镜像
        :param request:
        :param args:
        :param kwargs:
        :return:
        """
        user = request.user
        image_name = request.POST.get("image_name", "")
        image_vul_name = request.POST.get("image_vul_name", "")
        image_desc = request.POST.get("image_desc", "")
        try:
            image_rank = request.POST.get("rank", default=2.5)
            image_rank = float(image_rank)
        except:
            image_rank = 2.5
        image_file = request.FILES.get("file")
        image_info = None
        if image_name:
            if ":" not in image_name:
                image_name += ":latest"
            image_info = ImageInfo.objects.filter(image_name=image_name).first()
        if not image_info:
            image_info = ImageInfo(image_name=image_name, image_vul_name=image_vul_name, image_desc=image_desc,
                                   rank=image_rank, is_ok=False, create_date=timezone.now(), update_date=timezone.now())
            if not image_file:
                image_info.save()
        task_id = tasks.create_image_task(image_info=image_info, user_info=user, request_ip=get_request_ip(request),
                                          image_file=image_file)
        if image_file:
            task_info = TaskInfo.objects.filter(task_id=task_id).first()
            task_msg = task_info.task_msg
            return JsonResponse(json.loads(task_msg))
        else:
            pass
        return JsonResponse(R.ok(task_id, msg="拉取镜像 %s 任务下发成功" % (image_name, )))

    @action(methods=["get"], detail=True, url_path="delete")
    def delete_image(self, request, pk=None):
        user = request.user
        if not user.is_superuser:
            return JsonResponse(R.build(msg="权限不足"))
        img_info = ImageInfo.objects.filter(image_id=pk).first()
        if not img_info:
            return JsonResponse(R.ok())
        operation_args = ImageInfoSerializer(img_info).data
        request_ip = get_request_ip(request)
        sys_log = SysLog(user_id=user.id, operation_type="镜像", operation_name="删除",
                         operation_value=operation_args["image_vul_name"], operation_args=operation_args, ip=request_ip)
        sys_log.save()
        image_id = img_info.image_id
        container_vul = ContainerVul.objects.filter(image_id=image_id)
        if container_vul.count() == 0:
            img_info.delete()
            return JsonResponse(R.ok())
        else:
            return JsonResponse(R.build(msg="镜像正在使用，无法删除！"))

    @action(methods=["post", "get"], detail=True, url_path="start")
    def start_container(self, request, pk=None):
        """
        启动靶场
        :param request:
        :param pk:
        :return:
        """
        img_info = self.get_object()
        # 当前用户登录ID
        user = request.user
        image_id = img_info.image_id
        user_id = user.id
        container_vul = ContainerVul.objects.filter(user_id=user_id, image_id=image_id, time_model_id="").first()
        if not container_vul:
            container_vul = ContainerVul(image_id=img_info, user_id=user_id, vul_host="", container_status="stop",
                                         docker_container_id="",
                                         vul_port="",
                                         container_port="",
                                         time_model_id="",
                                         create_date=django.utils.timezone.now(),
                                         container_flag="")
            container_vul.save()
        task_id = tasks.create_container_task(container_vul, user, get_request_ip(request))
        return JsonResponse(R.ok(task_id))


class ContainerVulViewSet(viewsets.ReadOnlyModelViewSet):

    serializer_class = ContainerVulSerializer

    def get_queryset(self):
        request = self.request
        user = request.user
        flag = request.GET.get("flag", "")
        if flag == 'list' and user.is_superuser:
            container_vul_list = ContainerVul.objects.all()
        else:
            container_vul_list = ContainerVul.objects.all().filter(user_id=self.request.user.id, time_model_id="")
        return container_vul_list

    @action(methods=["get"], detail=True, url_path='start')
    def start_container(self, request, pk=None):
        """
        启动容器
        :param request:
        :param pk:
        :return:
        """
        user_info = request.user
        container_vul = self.get_object()
        task_id = tasks.create_container_task(container_vul=container_vul, user_info=user_info,
                                              request_ip=get_request_ip(request))
        return JsonResponse(R.ok(task_id))

    @action(methods=["get"], detail=True, url_path='stop')
    def stop_container(self, request, pk=None):
        """
        停止容器运行
        :param request:
        :param pk:
        :return:
        """
        user_info = request.user
        container_vul = self.get_object()
        task_id = tasks.stop_container_task(container_vul=container_vul, user_info=user_info,
                                            request_ip=get_request_ip(request))
        return JsonResponse(R.ok(task_id))

    '''
    删除容器
    '''
    @action(methods=["delete"], detail=True, url_path="delete")
    def delete_container(self, request, pk=None):
        user_info = request.user
        container_vul = self.get_object()
        # user_id = user_info.id
        task_id = tasks.delete_container_task(container_vul=container_vul, user_info=user_info,
                                              request_ip=get_request_ip(request))
        return JsonResponse(R.ok(task_id))

    '''
    验证Flag是否正确
    '''
    @action(methods=["post", "get"], detail=True, url_path="flag")
    def check_flag(self, request, pk=None):
        flag = request.GET.get('flag', None)
        container_vul = self.get_object()
        user_info = request.user
        user_id = user_info.id

        operation_args = ContainerVulSerializer(container_vul).data
        request_ip = get_request_ip(request)
        sys_log = SysLog(user_id=user_id, operation_type="容器", operation_name="提交Flag",
                         operation_value=operation_args["vul_name"], operation_args={"flag": flag},
                         ip=request_ip)
        sys_log.save()

        if user_id != container_vul.user_id:
            return JsonResponse(R.build(msg="Flag 与用户不匹配"))
        if not flag:
            return JsonResponse(R.build(msg="Flag不能为空"))
        if flag != container_vul.container_flag:
            return JsonResponse(R.build(msg="flag错误"))
        else:
            if not container_vul.is_check:
                # 更新为通过
                container_vul.is_check_date = timezone.now()
                container_vul.is_check = True
                container_vul.save()
                # 停止 Docker
                tasks.stop_container_task(container_vul=container_vul, user_info=user_info,
                                          request_ip=get_request_ip(request))
            return JsonResponse(R.ok())


class SysLogSet(viewsets.ModelViewSet):

    serializer_class = SysLogSerializer

    def get_queryset(self):
        request = self.request
        user = request.user
        if user.is_superuser:
            return SysLog.objects.all().filter()
        else:
            return []


def get_local_ip():
    """
    获取本机IP
    :return:
    """
    local_ip = ''
    if VUL_IP:
        return VUL_IP
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        local_ip = s.getsockname()[0]
    finally:
        s.close()
    return local_ip
