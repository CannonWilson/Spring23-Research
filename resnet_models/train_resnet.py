import os
import numpy as np
from dotenv import load_dotenv
import torch
from torch.cuda.amp import GradScaler, autocast
from torch.optim import SGD, lr_scheduler
import torch.nn as nn
from torch.utils.data import DataLoader
import torchvision
from torchvision import datasets, transforms
from mean_train import MEANS, STDEVS

load_dotenv()

DEVICE = f'cuda:{torch.cuda.device_count()-1}' if torch.cuda.is_available() else 'cpu'

# From paper
BATCH_SIZE = 512
EPOCHS = 30
PEAK_LR = 0.02
MOMENTUM = 0.9
WEIGHT_DECAY = 5e-4
PEAK_EPOCH = 2

# Other vars
DESTINATION_PATH = os.getenv("MODEL_PATH")
print(f'Beginning training. Saving model to {DESTINATION_PATH}')
LR_INIT= 0.5 # This is just a guess based on how initial LR for CIFAR was 0.5 in Example notebook
OUT_FEATS = 2 # CHANGED FROM 1
img_size = (int(os.getenv("IMG_WIDTH")), int(os.getenv("IMG_HEIGHT")))
assert img_size == (75, 75), "Images must be 75x75"
data_transforms = transforms.Compose([
    transforms.Resize(img_size),
    transforms.ToTensor(),
    transforms.Normalize(mean=list(MEANS.values()), std=list(STDEVS.values()))
])
def loader(dirn):
    """ 
    Create data using torchvision 
    ImageFolder and torch DataLoader.
    """
    return DataLoader(datasets.ImageFolder(dirn, transform=data_transforms), batch_size=BATCH_SIZE, shuffle=True)

TRAIN_DIR= os.getenv("TRAIN_DIR")
train_loader = DataLoader(datasets.ImageFolder(TRAIN_DIR, transform=data_transforms), batch_size=BATCH_SIZE, shuffle=True)

model = torchvision.models.resnet18()
# overwrite the last layer of resnet to use
# one output class (later, use bce)
model.fc = nn.Linear(in_features=512, out_features=OUT_FEATS, bias=True)
model.to(DEVICE)
model.train()

optimizer = SGD(model.parameters(),
                lr=LR_INIT,
                momentum=MOMENTUM,
                weight_decay=WEIGHT_DECAY)

# Implement a cyclic lr schedule
# credit: https://github.com/MadryLab/failure-directions/blob/d484125c5f5d0d7ec8666f5bfce9d496b2af83b9/failure_directions/src/optimizers.py#L1
iters_per_epoch = len(train_loader)
lr_schedule = np.interp(np.arange((EPOCHS+1) * iters_per_epoch),
                [0, PEAK_EPOCH * iters_per_epoch, EPOCHS * iters_per_epoch],
                [0, 1, 0])
def get_lr(epo):
    global lr_schedule
    return lr_schedule[epo]
scheduler = lr_scheduler.LambdaLR(optimizer, get_lr)
scaler = GradScaler()
# bce_loss = nn.BCEWithLogitsLoss() # nn.BCEWithLogitsLoss(reduction='none')
ce_loss = nn.CrossEntropyLoss()
sigmoid = nn.Sigmoid()
softmax = nn.Softmax()

for epoch in range(EPOCHS):
    epoch_loss = 0
    epoch_correct = 0
    epoch_total = 0
    for idx, (images, labels) in enumerate(train_loader):
        optimizer.zero_grad(set_to_none=True)
        images = images.to(DEVICE)
        labels = labels.to(DEVICE)
        with autocast():
            logits = model(images) # model(images).squeeze()
            loss = ce_loss(logits, labels.long()) # bce_loss(logits, labels.float())
            epoch_loss += loss
        scaler.scale(loss).backward()
        scaler.step(optimizer)
        scaler.update()
        scheduler.step()

        # pred = sigmoid(logits) > 0.5
        pred = torch.argmax(logits, dim=1)
        correct = pred == labels
        epoch_correct += correct.sum()
        epoch_total += labels.size()[0]
    acc = epoch_correct / epoch_total
    print('#### epoch: ', epoch+1,' #### ')
    print('loss: ', loss)
    print('acc: ', acc)
    torch.save(model.state_dict(), DESTINATION_PATH)