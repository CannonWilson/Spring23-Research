"""
This file trains an SVM using the CLIP embeddings 
of the validation set for each class (young/old)
as the *input* and the correctness (-1 or 1)
of the original ResNet classifier on each validation
image as the *labels*.

Then, the SVMs are used to calculate a decision
score for each test image of that class. The
test images are ordered by decision score and 
by the original classifier's confidences to see
which metric does a better job of surfacing 
the minority subgroup(s) when ordering test images
by that metric. Oh also PLOTS! :)
"""

import os
import torch
from torch.utils.data import DataLoader
from torch import nn
import torchvision
from torchvision import datasets, transforms
import clip
import numpy as np
import matplotlib.pyplot as plt
from PIL import Image
from sklearn import svm
from settings import NUM_CORRS, MODEL_PATH, IMG_WIDTH, IMG_HEIGHT, \
    TRAIN_MEANS_1_CORR, TRAIN_MEANS_2_CORR, TRAIN_STDEVS_1_CORR, \
    TRAIN_STDEVS_2_CORR, VAL_DIR, TEST_DIR

assert NUM_CORRS in [1,2], \
    "Only 1 or 2 correlations currently supported."

# Vars - Modify these to change experiment behavior
CALC_SVM_ACC = True
SAVE_FIGS = True
BATCH_SIZE = 512
DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
MODES = ["old", "young"]

print('Initializing Models and Loaders')

# Custom model
OUT_FEATS = 2
custom_model = torchvision.models.resnet18()
custom_model.fc = nn.Linear(in_features=512, out_features=OUT_FEATS, bias=True)
custom_model.load_state_dict(torch.load(MODEL_PATH, map_location=DEVICE))
custom_model.to(DEVICE)

img_size = (IMG_WIDTH, IMG_HEIGHT)
assert img_size == (75, 75), "Images must be 75x75"
MEANS = TRAIN_MEANS_1_CORR if NUM_CORRS == 1 else TRAIN_MEANS_2_CORR
STDEVS = TRAIN_STDEVS_1_CORR if NUM_CORRS == 1 else TRAIN_STDEVS_2_CORR
data_transforms = transforms.Compose([
    transforms.Resize(img_size),
    transforms.ToTensor(),
    transforms.Normalize(mean=list(MEANS.values()), std=list(STDEVS.values()))
])

# SVMs are trained on *val* set, Top_K is evaluated on *test* set
# the loaders with batch size of 1 is a convenient way to get all
# of the paths for images in a specific class
val_loader_no_trans = DataLoader(datasets.ImageFolder(VAL_DIR), batch_size=1)
NUM_VAL_IMGS = len(val_loader_no_trans)
test_loader_no_trans = DataLoader(datasets.ImageFolder(TEST_DIR), batch_size=1)
trained_svms = []

# CLIP
EMBEDDING_DIM = 512
clip_model, clip_preprocess = clip.load("ViT-B/32", device=DEVICE)

for mode in MODES:

    current_class_num = 1 if mode == 'young' else 0
    paths = [tup[0] for tup in val_loader_no_trans.dataset.samples \
                if tup[1] == current_class_num]
    num_imgs_this_class = len(paths)

    with torch.no_grad():
        print(f'Finding model correctness and clip embeds for {mode.upper()} val images')
        custom_model.eval()
        clip_model.eval()
        correctness = torch.empty(num_imgs_this_class, dtype=torch.int8, device=DEVICE)
        img_feature_stack = torch.empty(num_imgs_this_class, EMBEDDING_DIM)
        pil_images = []
        for path in paths:
            pil_images.append(clip_preprocess(Image.open(path)))
        cur_idx = 0
        cur_class_val_dir = os.path.join(VAL_DIR, mode) # ex: val/old
        cur_class_val_data = datasets.ImageFolder(cur_class_val_dir, transform=data_transforms)
        cur_class_val_loader = DataLoader(cur_class_val_data, batch_size=BATCH_SIZE)
        for i, (images, labels) in enumerate(cur_class_val_loader):
            # Calc classifier correctness for batch
            images = images.to(DEVICE)
            labels = labels.to(DEVICE)
            b_size = labels.size()[0]
            model_output = custom_model(images)
            preds = torch.argmax(model_output, dim=1)
            correct = torch.where(preds==labels, 1, -1)
            correctness[cur_idx:cur_idx+b_size] = correct

            # Get CLIP embeds for batch
            image_input = torch.tensor(np.stack(
                pil_images[cur_idx:cur_idx+b_size]), device=DEVICE)
            img_feature_stack[cur_idx:cur_idx+b_size] = \
                clip_model.encode_image(image_input).float()

            cur_idx += b_size
        del cur_class_val_data,cur_class_val_loader

    print('Finished getting clip embeddings and correctness scores.')
    print('Beginning to fit SVM classifier for class ', mode)
    svm_classifier = svm.SVC(kernel="linear") # LinearSVC(max_iter=5000) had worse performance
    np_feat_stack = img_feature_stack.cpu().numpy() # using StandardScaler() decreased performance
    np_corr = np.array(correctness.cpu(), dtype=np.int8)
    svm_classifier.fit(np_feat_stack, np_corr)
    trained_svms.append(svm_classifier)

assert len(MODES) == len(trained_svms), \
    "Number of fitted SVMs not equal to number of classes"

print("Finished training SVMs on validation data.")
for mode, svm_c in zip(MODES, trained_svms):

    current_class_num = 1 if mode == 'young' else 0
    test_paths = [tup[0] for tup in test_loader_no_trans.dataset.samples \
                    if tup[1] == current_class_num]
    IMGS_THIS_CLASS = len(test_paths)
    confidences = torch.empty(IMGS_THIS_CLASS)
    if CALC_SVM_ACC:
        test_correctness = torch.empty(IMGS_THIS_CLASS)
    ds_values = None
    pil_images = []
    sexes = np.empty(IMGS_THIS_CLASS)
    smiles = np.empty(IMGS_THIS_CLASS) if NUM_CORRS == 2 else None

    with torch.no_grad():
        print('Calculating model confidences for test images in class ', mode)
        cur_idx = 0
        cur_class_test_dir = os.path.join(TEST_DIR, mode) # ex: test/old
        cur_class_test_data = datasets.ImageFolder(cur_class_test_dir, transform=data_transforms)
        cur_class_test_loader = DataLoader(cur_class_test_data, batch_size=BATCH_SIZE)
        for i, (images, labels) in enumerate(cur_class_test_loader):
            images = images.to(DEVICE)
            labels = labels.to(DEVICE)
            b_size = labels.size()[0]
            model_output = custom_model(images)
            conf = torch.max(model_output, dim=1).values
            confidences[cur_idx:cur_idx+b_size] = conf
            if CALC_SVM_ACC:
                preds = torch.argmax(model_output, dim=1)
                correct = torch.where(preds==labels, 1, -1)
                test_correctness[cur_idx:cur_idx+b_size] = correct
            cur_idx += b_size

    with torch.no_grad():
        print("Getting CLIP embeddings, attributes, and decision scores " +\
                "for test images in class ", mode)
        for i, path in enumerate(test_paths):
            pil_images.append(clip_preprocess(Image.open(path)))
            # Also record sex/smiling of this test image
            sexes[i] = 0 if 'female' in path else 1
            if NUM_CORRS == 2:
                smiles[i] = 0 if 'no_smile' in path else 1
        test_feat_stack = torch.empty(IMGS_THIS_CLASS, EMBEDDING_DIM, device=DEVICE)
        num_test_batches = int(np.ceil(IMGS_THIS_CLASS / BATCH_SIZE))
        cur_idx = 0
        for _ in range(num_test_batches):
            end_idx = cur_idx + BATCH_SIZE
            if end_idx > IMGS_THIS_CLASS:
                end_idx = IMGS_THIS_CLASS
            image_input = torch.stack(pil_images[cur_idx:end_idx])
            test_feat_stack[cur_idx:end_idx] = \
                clip_model.encode_image(image_input).float()
            cur_idx += BATCH_SIZE
        ds_values = np.dot(svm_c.coef_[0], \
            test_feat_stack.cpu().numpy().transpose()) + \
                svm_c.intercept_[0]

    if CALC_SVM_ACC:
        test_correctness = test_correctness.cpu().numpy()
        ds_correctness = np.where(ds_values >= 0, 1, -1) # equivalent to np.sign but no 0s
        total = len(test_correctness)
        corr = (test_correctness == ds_correctness).sum()
        print(f"SVM accuracy for class {mode}: {corr/total}")

    print('Plotting/saving results for class ', mode)
    conf_sorted_idxs =  np.argsort(confidences.cpu().numpy())
    ds_sorted_idxs = np.flip(np.argsort(ds_values))
    conf_sorted_sexes = sexes[conf_sorted_idxs]
    ds_sorted_sexes = sexes[ds_sorted_idxs]
    conf_sorted_frac_male = np.empty(IMGS_THIS_CLASS)
    ds_sorted_frac_male = np.empty(IMGS_THIS_CLASS)
    if NUM_CORRS == 2:
        conf_sorted_smiles = smiles[conf_sorted_idxs]
        ds_sorted_smiles = smiles[ds_sorted_idxs]
        conf_sorted_frac_smiles = np.empty(IMGS_THIS_CLASS)
        ds_sorted_frac_smiles = np.empty(IMGS_THIS_CLASS)

    for num_people in range(1, IMGS_THIS_CLASS+1):
        conf_num_males = conf_sorted_sexes[:num_people].sum()
        ds_num_males = ds_sorted_sexes[:num_people].sum()
        conf_sorted_frac_male[num_people-1] = conf_num_males / num_people
        ds_sorted_frac_male[num_people-1] = ds_num_males / num_people
        if NUM_CORRS == 2:
            conf_num_smiles = conf_sorted_smiles[:num_people].sum()
            ds_num_smiles = ds_sorted_smiles[:num_people].sum()
            conf_sorted_frac_smiles[num_people-1] = conf_num_smiles / num_people
            ds_sorted_frac_smiles[num_people-1] = ds_num_smiles / num_people

    if mode == "old":
        minority_sex = "Female"
        sex_y_conf = 1-conf_sorted_frac_male
        sex_y_ds = 1-ds_sorted_frac_male
        sex_baseline = 1 - (conf_num_males / IMGS_THIS_CLASS)
        if NUM_CORRS == 2:
            minority_smile = "Smiling"
            smi_y_conf = conf_sorted_frac_smiles
            smi_y_ds = ds_sorted_frac_smiles
            smi_baseline = conf_num_smiles / IMGS_THIS_CLASS

    elif mode == "young":
        minority_sex = "Male"
        sex_y_conf = conf_sorted_frac_male
        sex_y_ds = ds_sorted_frac_male
        sex_baseline = conf_num_males / IMGS_THIS_CLASS
        if NUM_CORRS == 2:
            minority_smile = "Not Smiling"
            smi_y_conf = 1-conf_sorted_frac_smiles
            smi_y_ds = 1-ds_sorted_frac_smiles
            smi_baseline = 1 - (conf_num_smiles / IMGS_THIS_CLASS)

    # Plot sex results for class
    plt.plot(range(IMGS_THIS_CLASS), sex_y_conf, color='g', label="Confidence")
    plt.plot(range(IMGS_THIS_CLASS), sex_y_ds, color='b', label="Decision Score")
    plt.axhline(y=sex_baseline, color='r', label="Baseline")
    plt.ylabel(f'Fraction {minority_sex}')
    plt.xlabel("Top K Flagged")
    plt.legend(loc="upper right")
    plt.title(f"{minority_sex} Flagged for Class {mode}")
    if SAVE_FIGS: plt.savefig(f'new_{mode}_sex_{NUM_CORRS}_corr.png')
    plt.show()
    plt.clf()
    plt.close()

    # Plot smiling results for class if needed
    if NUM_CORRS == 2:
        # Plot smiling results for class
        plt.plot(range(IMGS_THIS_CLASS), smi_y_conf, color='g', label="Confidence")
        plt.plot(range(IMGS_THIS_CLASS), smi_y_ds, color='b', label="Decision Score")
        plt.axhline(y=smi_baseline, color='r', label="Baseline")
        plt.ylabel(f'Fraction {minority_smile}')
        plt.xlabel("Top K Flagged")
        plt.legend(loc="upper right")
        plt.title(f"{minority_smile} Flagged for Class {mode}")
        if SAVE_FIGS: plt.savefig(f'new_{mode}_smiling_{NUM_CORRS}_corr.png')
        plt.show()
        plt.clf()
        plt.close()
